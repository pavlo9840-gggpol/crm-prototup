"""Business logic. All stock changes go through add_movement()."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.utils import timezone
from . import cash as cashlib
from .models import (Shift, CashOperation, Customer, Product, PurchaseItem, Sale, SaleItem, SaleReturn,
                     SaleReturnItem, StockMovement, Purchase, PriceHistory, MarkupTier,
                     CompanySettings, CompanyMembership, norm_sku)

D0 = Decimal("0")


class StockError(Exception):
    pass


def _lock_products(product_ids, company=None):
    """Lock multiple stock rows in primary-key order to avoid cross-operation deadlocks."""
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    qs = Product.objects.select_for_update().filter(pk__in=ids)
    if company is not None:
        qs = qs.filter(company=company)
    return {p.pk: p for p in qs.order_by("pk")}


def add_movement(product, change, mtype, doc="", user=None, note=""):
    """The ONLY place that changes Product.current_stock."""
    p = Product.objects.select_for_update().get(pk=product.pk, company_id=product.company_id)
    p.current_stock += Decimal(change)
    p.save(update_fields=["current_stock"])
    return StockMovement.objects.create(company=p.company, product=p, movement_type=mtype, quantity_change=change,
                                        balance_after=p.current_stock, doc_ref=doc, note=note, user=user)


def _num(obj, prefix):
    obj.number = f"{prefix}-{obj.pk:06d}"
    obj.save(update_fields=["number"])


def _customer_delta(customer, amount, count, company=None):
    if customer:
        c = Customer.objects.select_for_update().get(pk=customer.pk, **({"company": company} if company is not None else {}))
        c.total_spent += amount
        c.purchases_count = max(0, c.purchases_count + count)
        c.save(update_fields=["total_spent", "purchases_count"])


def markup_price(cost, pct):
    """Ціна продажу = закупівельна + націнка %, до копійок."""
    return (Decimal(cost) * (Decimal(100) + Decimal(pct)) / Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def tier_for(customer, company=None):
    """Націнка клієнта; якщо не задана — типова; якщо немає й такої — 0 %."""
    company = company or (customer.company if customer is not None else None)
    if customer and customer.markup_tier_id:
        tier = customer.markup_tier
        if company is None or tier.company_id == company.pk:
            return tier
    qs = MarkupTier.objects.filter(is_default=True)
    if company is not None:
        qs = qs.filter(company=company)
    return qs.first()


def tier_pct(customer, company=None):
    t = tier_for(customer, company=company)
    return t.percent if t else D0


def ensure_product(si):
    """Для позиції з прайсу створює (або знаходить) технічну картку товару із залишком 0.
    Картка потрібна, щоб продаж, історія й повернення працювали як зі звичайним товаром;
    у каталозі каси вона з'являється лише коли товар справді оприбутковано."""
    if si.product_id:
        if si.product.company_id != si.company_id:
            raise StockError("Картка товару позиції постачальника належить іншому магазину")
        return si.product
    p = (Product.objects.filter(company=si.company, sku_norm=si.sku_norm, brand__iexact=si.brand).first()
         or Product.objects.filter(company=si.company, sku_norm=si.sku_norm, brand="").first())
    if not p:
        p = Product.objects.create(company=si.company, name=si.name or si.sku, sku=si.sku, brand=si.brand, cost_price=si.cost,
                                   price=markup_price(si.cost, tier_pct(None, company=si.company)), from_supplier=True)
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
            company=sale.company, sale=sale, product=p, quantity=q, original_price=x["list"], price=pr, cost_price=si.cost,
            source=SaleItem.SUPPLIER, supplier=si.supplier, supplier_item=si,
            fulfil_status=SaleItem.ORDERED if done else "",
            markup_pct=((pr / si.cost - 1) * 100).quantize(Decimal("0.01")) if si.cost else None)
    return SaleItem.objects.create(company=sale.company, sale=sale, product=p, quantity=q, original_price=p.price,
                                   price=pr, cost_price=p.cost_price, earned_at=timezone.now() if done else None)


def _can_manage(user, company):
    return CompanyMembership.objects.filter(company=company, user=user, is_active=True,
                                            role__in=("owner", "admin", "manager")).exists()


def save_held(user, lines, payment, customer, shift, held=None, car=None, company=None):
    company = company or (shift.company if shift else (lines[0][0].company if lines else None))
    if company is None or any(p.company_id != company.pk for p, *_ in lines):
        raise StockError("Товари належать іншому магазину або магазин не вибрано")
    if customer and customer.company_id != company.pk:
        raise StockError("Клієнт належить іншому магазину")
    if car and (car.company_id != company.pk or not customer or car.customer_id != customer.pk):
        raise StockError("Автомобіль не належить вибраному клієнту цього магазину")
    if shift and shift.company_id != company.pk:
        raise StockError("Касова зміна належить іншому магазину")
    if shift and shift.cashier_id != user.pk and not _can_manage(user, company):
        raise StockError("Немає доступу до цієї касової зміни")
    if held and held.company_id != company.pk:
        raise StockError("Чернетка належить іншому магазину")
    if held and held.cashier_id != user.pk and not _can_manage(user, company):
        raise StockError("Немає доступу до цієї чернетки")
    if any(x and (x["si"].company_id != company.pk or x["si"].supplier.company_id != company.pk) for *_a, x in lines):
        raise StockError("Позиція постачальника належить іншому магазину")
    sale = held or Sale(company=company, cashier=user, status=Sale.HELD)
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
def finalize_sale(user, lines, payment, customer, shift, held=None, pay=None, car=None, company=None):
    lines = merge_lines(lines)
    company = company or (shift.company if shift else (lines[0][0].company if lines else None))
    if company is None or any(p.company_id != company.pk for p, *_ in lines):
        raise StockError("Товари належать іншому магазину або магазин не вибрано")
    if customer and customer.company_id != company.pk:
        raise StockError("Клієнт належить іншому магазину")
    if shift and shift.company_id != company.pk:
        raise StockError("Касова зміна належить іншому магазину")
    if shift and shift.cashier_id != user.pk and not _can_manage(user, company):
        raise StockError("Немає доступу до цієї касової зміни")
    if any(x and (x["si"].company_id != company.pk or x["si"].supplier.company_id != company.pk) for *_a, x in lines):
        raise StockError("Позиція постачальника належить іншому магазину")
    if not lines:
        raise StockError("Чек порожній")
    if held is not None:
        held = Sale.objects.select_for_update().filter(pk=held.pk, company=company).first()
        if held is None or held.status != Sale.HELD:
            raise StockError("Цю чернетку вже проведено або видалено. Створіть новий чек.")
        if held.cashier_id != user.pk and not _can_manage(user, company):
            raise StockError("Немає доступу до цієї чернетки")
    if shift is not None:
        if shift.cashier_id != user.pk and not _can_manage(user, company):
            raise StockError("Немає доступу до цієї касової зміни")
        shift = Shift.objects.select_for_update().filter(pk=shift.pk, company=company, closed_at__isnull=True).first()
        if shift is None:
            raise StockError("Зміну вже закрито. Відкрийте нову зміну.")
    need = {}
    for p, q, _pr, x in lines:
        if not x:                                   # товар постачальника склад не перевіряє
            need[p.pk] = need.get(p.pk, D0) + q
    # A cart can contain several stock products. Acquire every row lock in
    # the same order used by purchases and adjustments, regardless of cart order.
    locked_products = _lock_products(need, company=company)
    if set(locked_products) != set(need):
        raise StockError("Один із товарів не належить цьому магазину")
    for pk, q in need.items():
        fresh = locked_products[pk]
        if fresh.current_stock < q:
            raise StockError(f"Товару {fresh.name} недостатньо, поточний залишок {fresh.current_stock.normalize():f}")
    sale = held or Sale(company=company, cashier=user)
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
    _customer_delta(customer, sale.total, 1, company=company)
    return sale


@transaction.atomic
def create_return(user, sale, qty_map, mode="auto", mtype="customer_return",
                  refund_method="pending", refund_cash_amount=None, shift=None, company=None):
    """Create a stock return and optionally settle its refund.

    Internal transit returns stay pending by default: they reverse the sale
    value but do not pretend that money was physically paid out.
    """
    company = company or sale.company
    if company is None or sale.company_id != company.pk:
        raise StockError("Продаж належить іншому магазину")
    if mode not in {"auto", "stock", "nostock"}:
        raise StockError("Невідомий режим повернення")
    if refund_method not in {"pending", "cash", "card", "mixed"}:
        raise StockError("Невідомий спосіб повернення коштів")

    sale = Sale.objects.select_for_update().filter(pk=sale.pk, company=company, status=Sale.DONE).first()
    if sale is None:
        raise StockError("Повернення можливе лише для проведеного продажу")

    # Lock the shift before touching product rows. Checkout uses the same
    # shift -> product lock order, which prevents a return/checkout deadlock.
    locked_shift = None
    if refund_method in {"cash", "mixed"}:
        if shift is None:
            raise StockError("Для повернення готівки спочатку відкрийте касову зміну")
        if shift.cashier_id != user.pk and not _can_manage(user, company):
            raise StockError("Немає доступу до цієї касової зміни")
        locked_shift = Shift.objects.select_for_update().filter(
            pk=shift.pk, company=company, closed_at__isnull=True
        ).first()
        if locked_shift is None:
            raise StockError("Касова зміна вже закрита. Відкрийте нову зміну")

    total = D0
    ret = SaleReturn.objects.create(company=company, sale=sale, user=user)
    ret.number = f"PV-{ret.pk:06d}"
    ret.save(update_fields=["number"])

    for item in sale.items.select_for_update().filter(company=company).order_by("pk"):
        if item.product.company_id != company.pk:
            raise StockError("У чеку виявлено товар іншого магазину")
        q = qty_map.get(item.pk, D0)
        if q <= 0:
            continue
        if q > item.returnable:
            raise StockError(f"{item.product.name}: можна повернути не більше {item.returnable.normalize():f}")
        item.returned_qty += q
        item.save(update_fields=["returned_qty"])
        SaleReturnItem.objects.create(company=company, sale_return=ret, sale_item=item, quantity=q, price=item.price)
        total += q * item.price
        pending = item.is_pending
        if mode == "stock" or (mode == "auto" and not pending):
            add_movement(item.product, q, mtype, ret.number, user)
        if pending and item.returned_qty >= item.quantity:
            item.fulfil_status = SaleItem.CANCELLED
            item.save(update_fields=["fulfil_status"])

    if total <= 0:
        raise StockError("Не вибрано жодного товару для повернення")

    cash_amount = D0
    card_amount = D0
    if refund_method == "cash":
        cash_amount = total
    elif refund_method == "card":
        card_amount = total
    elif refund_method == "mixed":
        try:
            cash_amount = Decimal(str(refund_cash_amount)).quantize(Decimal("0.01"))
        except Exception:
            raise StockError("Вкажіть коректну суму повернення готівкою")
        card_amount = total - cash_amount
        if cash_amount <= 0 or card_amount <= 0:
            raise StockError("Для змішаного повернення обидві суми мають бути більшими за нуль")

    if cash_amount > 0:
        if locked_shift is None:
            raise StockError("Для повернення готівки потрібна відкрита касова зміна")
        drawer = cashlib.clean(locked_shift.drawer)
        payout = cashlib.make_change(cashlib.to_kop(cash_amount), drawer)
        if payout is None:
            raise StockError("У касі недостатньо купюр/монет для точної суми повернення. Оберіть повернення на картку або змініть суму")
        locked_shift.drawer = cashlib.store(cashlib.add(drawer, payout, -1))
        locked_shift.save(update_fields=["drawer"])
        CashOperation.objects.create(
            company=company, shift=locked_shift, user=user, kind="out", amount=-cash_amount,
            detail={str(denom): -count for denom, count in payout.items()},
            note=f"Повернення {ret.number}",
        )

    ret.total = total
    ret.refund_method = refund_method
    ret.cash_amount = cash_amount
    ret.card_amount = card_amount
    ret.shift = locked_shift
    ret.save(update_fields=["total", "refund_method", "cash_amount", "card_amount", "shift"])
    _customer_delta(sale.customer, -total, 0, company=company)
    return ret



@transaction.atomic
def settle_return(user, sale_return, refund_method, refund_cash_amount=None, shift=None, company=None):
    """Settle a previously recorded return that is still awaiting payment."""
    if refund_method not in {"cash", "card", "mixed"}:
        raise StockError("Оберіть спосіб виплати: готівка, картка або змішано")
    company = company or sale_return.company
    if company is None or sale_return.company_id != company.pk:
        raise StockError("Повернення належить іншому магазину")
    ret = SaleReturn.objects.select_for_update().filter(pk=sale_return.pk, company=company, sale__company=company).first()
    if ret is None:
        raise StockError("Повернення не знайдено")
    if ret.refund_method != "pending":
        raise StockError("Це повернення вже має зафіксований спосіб виплати")

    cash_amount = D0
    card_amount = D0
    if refund_method == "cash":
        cash_amount = ret.total
    elif refund_method == "card":
        card_amount = ret.total
    else:
        try:
            cash_amount = Decimal(str(refund_cash_amount)).quantize(Decimal("0.01"))
        except Exception:
            raise StockError("Вкажіть коректну суму повернення готівкою")
        card_amount = ret.total - cash_amount
        if cash_amount <= 0 or card_amount <= 0:
            raise StockError("Для змішаної виплати обидві суми мають бути більшими за нуль")

    locked_shift = None
    if cash_amount > 0:
        if shift is None:
            raise StockError("Для повернення готівки спочатку відкрийте касову зміну")
        if shift.cashier_id != user.pk and not _can_manage(user, company):
            raise StockError("Немає доступу до цієї касової зміни")
        locked_shift = Shift.objects.select_for_update().filter(
            pk=shift.pk, company=company, closed_at__isnull=True
        ).first()
        if locked_shift is None:
            raise StockError("Касова зміна вже закрита. Відкрийте нову зміну")
        drawer = cashlib.clean(locked_shift.drawer)
        payout = cashlib.make_change(cashlib.to_kop(cash_amount), drawer)
        if payout is None:
            raise StockError("У касі недостатньо купюр/монет для точної суми повернення")
        locked_shift.drawer = cashlib.store(cashlib.add(drawer, payout, -1))
        locked_shift.save(update_fields=["drawer"])
        CashOperation.objects.create(
            company=company, shift=locked_shift, user=user, kind="out", amount=-cash_amount,
            detail={str(denom): -count for denom, count in payout.items()},
            note=f"Виплата повернення {ret.number}",
        )

    ret.refund_method = refund_method
    ret.cash_amount = cash_amount
    ret.card_amount = card_amount
    ret.shift = locked_shift
    ret.save(update_fields=["refund_method", "cash_amount", "card_amount", "shift"])
    return ret


@transaction.atomic
def create_purchase(user, supplier, lines, update_cost=True, company=None):
    company = company or supplier.company
    if company is None or supplier.company_id != company.pk or any(p.company_id != company.pk for p, *_ in lines):
        raise StockError("Постачальник або товар належить іншому магазину")
    if not lines:
        raise StockError("Додайте хоча б один товар")
    # Validate every line before creating the document or touching stock.
    # Forms normally enforce this, but the service is also a business-rule boundary.
    for p, q, cost in lines:
        if q is None or q <= D0:
            raise StockError(f"{p.name}: кількість має бути більшою за нуль")
        if cost is None or cost < D0:
            raise StockError(f"{p.name}: закупівельна ціна не може бути від'ємною")
    # Lock all affected stock rows before changing any of them. Sorting makes
    # two purchases with the same products acquire locks in a consistent order.
    locked = _lock_products((p.pk for p, _q, _cost in lines), company=company)
    if len(locked) != len({p.pk for p, _q, _cost in lines}):
        raise StockError("Один із товарів не належить цьому магазину")
    pur = Purchase.objects.create(company=company, supplier=supplier, user=user)
    _num(pur, "NK")
    total = D0
    for product, q, cost in lines:
        # Use the locked, fresh product row for both stock and cost updates. The
        # caller may have supplied a stale Product instance, and one purchase can
        # contain the same product more than once.
        movement = add_movement(product, q, "purchase", pur.number, user)
        p = movement.product
        PurchaseItem.objects.create(company=company, purchase=pur, product=p, quantity=q, cost_price=cost)
        total += q * cost
        if update_cost and p.cost_price != cost:
            PriceHistory.objects.create(company=company, product=p, field="cost_price", old_value=p.cost_price,
                                        new_value=cost, changed_by=user)
            p.cost_price = cost
            p.save(update_fields=["cost_price"])
        if p.auto_price:                                  # автоціна має рахуватись від актуальної закупівельної
            from . import pricing
            pricing.reprice_product(p, user)
    pur.total = total
    pur.save(update_fields=["total"])
    return pur


@transaction.atomic
def apply_adjustment(user, adj_form, company=None):
    adj = adj_form.save(commit=False)
    company = company or getattr(adj.product, "company", None)
    if company is None or adj.product.company_id != company.pk:
        raise StockError("Товар належить іншому магазину")
    adj.company = company
    adj.user = user
    sign = 1 if adj.kind == "surplus" else -1
    p = Product.objects.select_for_update().get(pk=adj.product.pk, company=company)
    if sign < 0 and p.current_stock < adj.quantity:
        raise StockError(f"Недостатньо залишку ({p.current_stock.normalize():f})")
    adj.save()
    add_movement(p, sign * adj.quantity, adj.kind, f"ADJ-{adj.pk:06d}", user,
                 note=(adj.get_reason_display() + " " + adj.note).strip())
    return adj


# ====================================================== замовлення в дорозі (товар постачальника)
def _own(user, item):
    return item.sale.cashier_id == user.id or _can_manage(user, item.sale.company)


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
    item = SaleItem.objects.select_for_update().select_related("sale").get(pk=item.pk, company=item.sale.company)
    _check(user, item, (SaleItem.ORDERED, SaleItem.ARRIVED), receiving)
    days = int(days) if days not in (None, "") else CompanySettings.get(item.sale.company).pickup_days
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
    item = SaleItem.objects.select_for_update().select_related("sale").get(pk=item.pk, company=item.company_id)
    _check(user, item, (SaleItem.ORDERED, SaleItem.ARRIVED))
    item.fulfil_status = SaleItem.ISSUED
    item.arrived_at = item.arrived_at or timezone.now()
    item.earned_at = timezone.now()                       # клієнт забрав: маржа зароблена
    item.save(update_fields=["fulfil_status", "arrived_at", "earned_at"])
    return item


def _unclaimed(user, item, mode):
    # Keep lock order consistent with create_return(): sale first, then its items.
    # Locking the item first here could deadlock against a simultaneous return.
    Sale.objects.select_for_update().get(pk=item.sale_id, company=item.company_id)
    item = SaleItem.objects.select_for_update().select_related("sale").get(pk=item.pk, company=item.company_id)
    _check(user, item, (SaleItem.ARRIVED,))
    before = Product.objects.select_for_update().get(pk=item.product_id, company=item.company_id).current_stock
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
