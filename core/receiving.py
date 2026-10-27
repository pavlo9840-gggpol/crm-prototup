"""Приймання накладної постачальника БЕЗ складовщика.

Кожен рядок накладної зіставляється за брендом і артикулом (без пробілів і дефісів):
  1. з відкритими замовленнями клієнтів у цього постачальника (за чергою: хто замовив раніше): позиція стає «Прибув»
     і запускається відлік на забір. Склад не чіпається: товар піде клієнту транзитом;
  2. залишок, під який немає замовлень, оприбутковується на склад (звичайна закупівля).
Якщо прийшло менше, ніж замовлено, позиція чека ділиться: частина, що прийшла, отримує статус «Прибув», решта лишається в очікуванні.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from . import services
from .crosses import analog_levels
from .models import (CompanySettings, Product, Receipt, Sale, SaleItem, SupplierItem, norm_sku)
from .services import StockError

D0 = Decimal("0")


def parse_lines(records, rate=None):
    """records: словники {sku, brand, name, qty, cost} (рядки). Однакові артикули зливаються (кількість додається).
    rate: курс, якщо ціни в накладній не в гривні (cost стає гривневим, оригінал лишається в cost_orig)."""
    from .transit import num
    merged, bad = {}, []
    for n, r in enumerate(records, 1):
        sku = (r.get("sku") or "").strip()
        if not sku and not any(str(v or "").strip() for v in r.values()):
            continue
        sn, qty = norm_sku(sku), num(r.get("qty"))
        if not sn or qty is None or qty <= 0:
            bad.append((n, sku or "?", "порожній артикул або кількість"))
            continue
        brand = " ".join((r.get("brand") or "").split()).upper()[:60]
        cost = num(r.get("cost")) or D0
        key = (brand, sn)
        if key in merged:
            m = merged[key]
            total = m["qty"] + qty
            m["cost"] = ((m["cost"] * m["qty"] + cost * qty) / total).quantize(Decimal("0.01")) if total else m["cost"]
            m["qty"] = total
        else:
            merged[key] = {"brand": brand, "sku": sku[:80], "sn": sn, "name": (r.get("name") or "").strip()[:250],
                           "qty": qty, "cost": cost.quantize(Decimal("0.01"))}
    lines = list(merged.values())
    if rate is not None and Decimal(rate) != 1:
        for ln in lines:
            ln["cost_orig"] = ln["cost"]
            ln["cost"] = (ln["cost"] * Decimal(rate)).quantize(Decimal("0.01"))
    return lines, bad


def _item_brand(it):
    return (it.supplier_item.brand if it.supplier_item_id else it.product.brand or "").upper()


def _open_orders(sup):
    """Відкриті замовлення клієнтів у постачальника: {(бренд, артикул_норм): [позиції за чергою]}."""
    qs = (SaleItem.objects.filter(source=SaleItem.SUPPLIER, supplier=sup, fulfil_status=SaleItem.ORDERED, sale__status=Sale.DONE)
          .select_related("sale__customer", "sale__cashier", "supplier_item", "product").order_by("sale__created_at", "pk"))
    out = defaultdict(list)
    for it in qs:
        out[(_item_brand(it), it.product.sku_norm)].append(it)
    return out


def _resolve_brand(sup, line, open_orders):
    """Бренд, якщо його не вказано: спершу за відкритими замовленнями (накладна найімовірніше закриває саме їх),
    потім за каталогом. Неоднозначність не вгадуємо: рядок буде пропущено з поясненням."""
    if line["brand"]:
        return line["brand"], None
    sn = line["sn"]
    ordered = {b for (b, n) in open_orders if n == sn}
    if len(ordered) == 1:
        return next(iter(ordered)), None
    cands = ordered or (set(SupplierItem.objects.filter(supplier=sup, sku_norm=sn).values_list("brand", flat=True))
                        | {b.upper() for b in Product.objects.filter(sku_norm=sn).values_list("brand", flat=True)})
    cands.discard("")
    if len(cands) == 1:
        return cands.pop(), None
    if not cands:
        return "", "бренд не вказано, і за артикулом його не визначити"
    return "", "артикул є в кількох брендів (" + ", ".join(sorted(cands)[:4]) + "), вкажіть бренд"


def plan(sup, lines):
    """Нічого не змінює: показує, що відбудеться. Повертає {rows, totals}."""
    open_orders = _open_orders(sup)
    used = defaultdict(lambda: D0)                      # скільки вже розподілено з кожної позиції чека
    rows, claimed = [], set()
    for ln in lines:
        brand, err = _resolve_brand(sup, ln, open_orders)
        row = {"line": ln, "brand": brand, "alloc": [], "stock_qty": D0, "warnings": [], "error": err, "cost_changes": 0}
        rows.append(row)
        if err:
            continue
        remaining = ln["qty"]
        for it in open_orders.get((brand, ln["sn"]), []):
            left = it.quantity - it.returned_qty - used[it.pk]
            if left <= 0 or remaining <= 0:
                continue
            take = min(left, remaining)
            if take < left and (it.returned_qty > 0 or used[it.pk] > 0):
                row["warnings"].append(f"{it.sale.number}: частково повернена позиція не ділиться, відкрийте її вручну")
                continue
            used[it.pk] += take
            remaining -= take
            claimed.add(it.pk)
            row["alloc"].append({"item": it, "qty": take, "split": take < left,
                                 "cost_diff": bool(ln["cost"] and abs(it.cost_price - ln["cost"]) > max(Decimal("0.01"), it.cost_price * Decimal("0.01")))})
        row["stock_qty"] = remaining
        row["cost_changes"] = sum(1 for a in row["alloc"] if a["cost_diff"])
        if remaining > 0 and not row["alloc"] and not open_orders.get((brand, ln["sn"])):
            for n, lvl in analog_levels(ln["sn"]).items():          # можлива заміна замість замовленого
                if lvl in ("oe", "manual", "cross"):
                    for (b2, n2), its in open_orders.items():
                        if n2 == n and its:
                            row["warnings"].append(f"Можливо, це заміна для замовлення {its[0].sale.number}: {b2} {its[0].product.sku} "
                                                   f"(аналог). Перевірте й за потреби оформіть вручну; зараз рядок піде на склад.")
                            break
        if remaining > 0 and not ln["cost"]:
            row["warnings"].append("у накладній немає ціни: на склад піде з нульовою закупівельною")
        if remaining > 0 and not Product.objects.filter(sku_norm=ln["sn"], brand__iexact=brand).exists():
            row["warnings"].append("нова деталь: на складі такої картки ще немає, її буде створено. Перевірте ціну продажу після приймання")
    totals = {"matched": sum(len(r["alloc"]) for r in rows), "stock_lines": sum(1 for r in rows if r["stock_qty"] > 0 and not r["error"]),
              "errors": sum(1 for r in rows if r["error"]), "pending_after": sum(len(v) for v in open_orders.values()) - len(claimed)}
    return {"rows": rows, "totals": totals}


def _split(it, take):
    """Ділить позицію чека: нова позиція на `take` одиниць (прибула), у старої лишається решта. Сума чека не змінюється."""
    new = SaleItem.objects.create(
        sale=it.sale, product=it.product, quantity=take, original_price=it.original_price, price=it.price,
        cost_price=it.cost_price, source=it.source, supplier=it.supplier, supplier_item=it.supplier_item,
        fulfil_status=SaleItem.ORDERED, markup_pct=it.markup_pct)
    it.quantity -= take
    it.save(update_fields=["quantity"])
    return new


@transaction.atomic
def execute(user, sup, number, days, lines):
    """Застосовує приймання (план перераховується всередині, щоб не покладатись на застарілі дані)."""
    pl = plan(sup, lines)
    number = (number or "").strip()[:60]
    days = days if days else CompanySettings.get().pickup_days
    n_matched = n_stock = n_skipped = 0
    stock_lines, notes = [], []
    for row in pl["rows"]:
        ln = row["line"]
        if row["error"]:
            n_skipped += 1
            notes.append(f"пропущено {ln['sku']}: {row['error']}")
            continue
        for a in row["alloc"]:
            it = SaleItem.objects.select_for_update().get(pk=a["item"].pk)
            if it.fulfil_status != SaleItem.ORDERED:
                raise StockError("Дані змінились під час приймання, повторіть")
            target = _split(it, a["qty"]) if a["split"] else it
            if ln["cost"] and a["cost_diff"]:
                target.cost_price = ln["cost"]
                target.save(update_fields=["cost_price"])
            services.mark_arrived(user, target, days, ref=number or f"приймання {sup.name}", receiving=True)
            n_matched += 1
            notes.append(f"{ln['brand']} {ln['sku']} × {a['qty'].normalize():f} → {it.sale.number}")
        if row["stock_qty"] > 0:
            prod = Product.objects.filter(sku_norm=ln["sn"], brand__iexact=row["brand"]).first()
            if prod is None:
                si = SupplierItem.objects.filter(supplier=sup, brand=row["brand"], sku_norm=ln["sn"]).first()
                if si:
                    prod = services.ensure_product(si)
                else:
                    cost = ln["cost"] or D0
                    prod = Product.objects.create(name=ln["name"] or f"{row['brand']} {ln['sku']}", sku=ln["sku"], brand=row["brand"],
                                                  cost_price=cost, price=services.markup_price(cost, services.tier_pct(None)))
            stock_lines.append((prod, row["stock_qty"], ln["cost"] or D0))
            n_stock += 1
            notes.append(f"{ln['brand']} {ln['sku']} × {row['stock_qty'].normalize():f} → склад")
    pur = services.create_purchase(user, sup, stock_lines) if stock_lines else None
    if not (n_matched or n_stock):
        raise StockError("Немає що приймати: жоден рядок не вдалося зіставити")
    return Receipt.objects.create(supplier=sup, number=number, user=user, n_matched=n_matched, n_stock=n_stock,
                                  n_skipped=n_skipped, purchase=pur, note="\n".join(notes)[:5000])
