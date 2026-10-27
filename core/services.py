"""Business logic. All stock changes go through add_movement()."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.utils import timezone
from . import cash as cashlib
from .models import (Shift, Customer, Product, PurchaseItem, Sale, SaleItem, SaleReturn,
                     SaleReturnItem, StockMovement, Purchase, PriceHistory, MarkupTier,
                     CompanySettings, norm_sku)

D0 = Decimal("0")


class StockError(Exception):
    pass


def add_movement(product, change, mtype, doc="", user=None, note=""):
    """The ONLY place that changes Product.current_stock."""
    p = Product.objects.select_for_update().get(pk=product.pk)
    p.current_stock += Decimal(change)
    p.save(update_fields=["current_stock"])
    return StockMovement.objects.create(product=p, movement_type=mtype, quantity_change=change,
                                        balance_after=p.current_stock, doc_ref=doc, note=note, user=user)


def _num(obj, prefix):
    obj.number = f"{prefix}-{obj.pk:06d}"
    obj.save(update_fields=["number"])


def _customer_delta(customer, amount, count):
    if customer:
        c = Customer.objects.select_for_update().get(pk=customer.pk)
        c.total_spent += amount
        c.purchases_count = max(0, c.purchases_count + count)
        c.save(update_fields=["total_spent", "purchases_count"])


def markup_price(cost, pct):
    """Ціна продажу = закупівельна + націнка %, до копійок."""
    return (Decimal(cost) * (Decimal(100) + Decimal(pct)) / Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def tier_for(customer):
    """Націнка клієнта; якщо не задана — типова; якщо немає й такої — 0 %."""
    if customer and customer.markup_tier_id:
        return customer.markup_tier
    return MarkupTier.objects.filter(is_default=True).first()


def tier_pct(customer):
    t = tier_for(customer)
    return t.percent if t else D0


def ensure_product(si):
    """Для позиції з прайсу створює (або знаходить) технічну картку товару із залишком 0.
    Картка потрібна, щоб продаж, історія й повернення працювали як зі звичайним товаром;
    у каталозі каси вона з'являється лише коли товар справді оприбутковано."""
    if si.product_id:
        return si.product
    p = (Product.objects.filter(sku_norm=si.sku_norm, brand__iexact=si.brand).first()
         or Product.objects.filter(sku_norm=si.sku_norm, brand="").first())
    if not p:
        p = Product.objects.create(name=si.name or si.sku, sku=si.sku, brand=si.brand, cost_price=si.cost,
                                   price=markup_price(si.cost, tier_pct(None)), from_supplier=True)
    si.product = p
    si.save(update_fields=["product"])
    return p


def merge_lines(lines):
    """Рядок = (товар, к-сть, ціна[, extra]); extra = {"si": SupplierItem} для товару постачальника.
    Рядок зі складу і рядок від постачальника з тим самим товаром лишаються окремими."""
    merged = {}
    for ln in lines:
        p, q, pr = ln[:3]
        x = ln[3] if len(ln) > 3 else None
        key = (p.pk, x["si"].pk if x else None)
        if key in merged:
            merged[key][1] += q
        else:
            merged[key] = [p, q, pr, x]
    return list(merged.values())


def _make_item(sale, p, q, pr, x, done):
    if x:
        si = x["si"]
        return SaleItem.objects.create(
            sale=sale, product=p, quantity=q, original_price=x["list"], price=pr, cost_price=si.cost,
            source=SaleItem.SUPPLIER, supplier=si.supplier, supplier_item=si,
            fulfil_status=SaleItem.ORDERED if done else "",
            markup_pct=((pr / si.cost - 1) * 100).quantize(Decimal("0.01")) if si.cost else None)
    return SaleItem.objects.create(sale=sale, product=p, quantity=q, original_price=p.price,
                                   price=pr, cost_price=p.cost_price, earned_at=timezone.now() if done else None)


@transaction.atomic
def save_held(user, lines, payment, customer, shift, held=None, car=None):
    sale = held or Sale(cashier=user, status=Sale.HELD)
    sale.shift, sale.customer, sale.payment_method = shift, customer, payment
    sale.car = car if customer else None
    sale.total = sum((q * pr for _, q, pr, _x in lines), D0)
    sale.save()
    if not sale.number:
        _num(sale, "CH")
    sale.items.all().delete()
    for p, q, pr, x in lines:
        _make_item(sale, p, q, pr, x, done=False)
    return sale


@transaction.atomic
def finalize_sale(user, lines, payment, customer, shift, held=None, pay=None, car=None):
    lines = merge_lines(lines)
    if not lines:
        raise StockError("Чек порожній")
    need = {}
    for p, q, _pr, x in lines:
        if not x:                                   # товар постачальника склад не перевіряє
            need[p.pk] = need.get(p.pk, D0) + q
    for pk, q in need.items():
        fresh = Product.objects.select_for_update().get(pk=pk)
        if fresh.current_stock < q:
            raise StockError(f"Товару {fresh.name} недостатньо, поточний залишок {fresh.current_stock.normalize():f}")
    sale = held or Sale(cashier=user)
    sale.status = Sale.DONE
    sale.shift, sale.customer, sale.payment_method = shift, customer, payment
    sale.car = car if customer else None
    sale.total = sum((q * pr for _, q, pr, _x in lines), D0)
    pay = pay or {}
    sale.card_amount = pay.get("card", D0 if payment != "card" else sale.total)
    sale.cash_amount = sale.total - sale.card_amount
    sale.cash_received = pay.get("received", D0)
    sale.change_given = pay.get("change", D0)
    sale.change_detail = pay.get("detail", {})
    sale.save()
    if not sale.number:
        _num(sale, "CH")
    if shift and pay.get("drawer") is not None:
        Shift.objects.filter(pk=shift.pk).update(drawer=cashlib.store(pay["drawer"]))
    sale.items.all().delete()
    for p, q, pr, x in lines:
        _make_item(sale, p, q, pr, x, done=True)
        if not x:
            add_movement(p, -q, "sale", sale.number, user)
    _customer_delta(customer, sale.total, 1)
    return sale


@transaction.atomic
def create_return(user, sale, qty_map, mode="auto", mtype="customer_return"):
    """mode: auto — на склад, окрім позицій постачальника, що ще не видані клієнту (їх просто скасовано);
    stock — завжди на склад; nostock — склад не чіпати."""
    ret = SaleReturn.objects.create(sale=sale, user=user)
    ret.number = f"PV-{ret.pk:06d}"
    ret.save(update_fields=["number"])
    total = D0
    for item in sale.items.select_for_update():
        q = qty_map.get(item.pk, D0)
        if q <= 0:
            continue
        if q > item.returnable:
            raise StockError(f"{item.product.name}: можна повернути не більше {item.returnable.normalize():f}")
        item.returned_qty += q
        item.save(update_fields=["returned_qty"])
        SaleReturnItem.objects.create(sale_return=ret, sale_item=item, quantity=q, price=item.price)
        total += q * item.price
        pending = item.is_pending
        if mode == "stock" or (mode == "auto" and not pending):
            add_movement(item.product, q, mtype, ret.number, user)
        if pending and item.returned_qty >= item.quantity:
            item.fulfil_status = SaleItem.CANCELLED
            item.save(update_fields=["fulfil_status"])
    if total <= 0:
        raise StockError("Не вибрано жодного товару для повернення")
    ret.total = total
    ret.save(update_fields=["total"])
    _customer_delta(sale.customer, -total, 0)
    return ret


@transaction.atomic
def create_purchase(user, supplier, lines, update_cost=True):
    if not lines:
        raise StockError("Додайте хоча б один товар")
    pur = Purchase.objects.create(supplier=supplier, user=user)
    _num(pur, "NK")
    total = D0
    for p, q, cost in lines:
        PurchaseItem.objects.create(purchase=pur, product=p, quantity=q, cost_price=cost)
        total += q * cost
        add_movement(p, q, "purchase", pur.number, user)
        if update_cost and p.cost_price != cost:
            PriceHistory.objects.create(product=p, field="cost_price", old_value=p.cost_price,
                                        new_value=cost, changed_by=user)
            Product.objects.filter(pk=p.pk).update(cost_price=cost)
        if p.auto_price:                                  # автоціна: нова закупівля → ціна за правилами
            from . import pricing
            pricing.reprice_product(p, user)
    pur.total = total
    pur.save(update_fields=["total"])
    return pur


@transaction.atomic
def apply_adjustment(user, adj_form):
    adj = adj_form.save(commit=False)
    adj.user = user
    sign = 1 if adj.kind == "surplus" else -1
    p = Product.objects.select_for_update().get(pk=adj.product.pk)
    if sign < 0 and p.current_stock < adj.quantity:
        raise StockError(f"Недостатньо залишку ({p.current_stock.normalize():f})")
    adj.save()
    add_movement(p, sign * adj.quantity, adj.kind, f"ADJ-{adj.pk:06d}", user,
                 note=(adj.get_reason_display() + " " + adj.note).strip())
    return adj


# ====================================================== замовлення в дорозі (товар постачальника)
def _own(user, item):
    return user.is_staff or item.sale.cashier_id == user.id


def _check(user, item, allowed, receiving=False):
    if item.source != SaleItem.SUPPLIER or item.sale.status != Sale.DONE:
        raise StockError("Це не позиція під замовлення")
    if not receiving and not _own(user, item):
        raise StockError("Це позиція іншого менеджера")
    if item.fulfil_status not in allowed:
        raise StockError("Дія недоступна для поточного статусу")


@transaction.atomic
def mark_arrived(user, item, days=None, ref="", receiving=False):
    """Товар прийшов від постачальника: запускаємо відлік на забір (за замовчуванням 14 днів)."""
    item = SaleItem.objects.select_for_update().select_related("sale").get(pk=item.pk)
    _check(user, item, (SaleItem.ORDERED, SaleItem.ARRIVED), receiving)
    days = int(days) if days not in (None, "") else CompanySettings.get().pickup_days
    if days < 1 or days > 365:
        raise StockError("Кількість днів відліку: від 1 до 365")
    item.fulfil_status = SaleItem.ARRIVED
    item.arrived_at = item.arrived_at or timezone.now()
    item.pickup_deadline = timezone.localdate() + timedelta(days=days)
    fields = ["fulfil_status", "arrived_at", "pickup_deadline"]
    if ref:
        item.receipt_ref = ref[:60]
        fields.append("receipt_ref")
    item.save(update_fields=fields)
    return item


@transaction.atomic
def mark_issued(user, item):
    """Клієнт забрав товар — транзитом, без складу."""
    item = SaleItem.objects.select_for_update().select_related("sale").get(pk=item.pk)
    _check(user, item, (SaleItem.ORDERED, SaleItem.ARRIVED))
    item.fulfil_status = SaleItem.ISSUED
    item.arrived_at = item.arrived_at or timezone.now()
    item.earned_at = timezone.now()                       # клієнт забрав: маржа зароблена
    item.save(update_fields=["fulfil_status", "arrived_at", "earned_at"])
    return item


def _unclaimed(user, item, mode):
    item = SaleItem.objects.select_for_update().select_related("sale").get(pk=item.pk)
    _check(user, item, (SaleItem.ARRIVED,))
    before = Product.objects.select_for_update().get(pk=item.product_id).current_stock
    qty = item.quantity - item.returned_qty
    if qty > 0:
        create_return(user, item.sale, {item.pk: qty}, mode=mode, mtype="transit_stock")
    return item, before


@transaction.atomic
def stock_unclaimed(user, item):
    """Клієнт не забрав: товар оприбутковується на склад, а позиція в чеку скасовується (повернення)."""
    item, before = _unclaimed(user, item, "stock")
    if before <= 0:        # на складі цього товару не було, тож собівартість береться з цієї партії
        Product.objects.filter(pk=item.product_id).update(cost_price=item.cost_price)
    SaleItem.objects.filter(pk=item.pk).update(fulfil_status=SaleItem.STOCKED)
    return item


@transaction.atomic
def return_to_supplier(user, item):
    """Клієнт не забрав: товар повертається постачальнику, склад не змінюється."""
    item, _ = _unclaimed(user, item, "nostock")
    SaleItem.objects.filter(pk=item.pk).update(fulfil_status=SaleItem.SUP_RETURN)
    return item
