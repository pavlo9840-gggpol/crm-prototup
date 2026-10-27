"""Валюти й курси. Ціни постачальників можуть бути в EUR/USD/PLN, а облік і продаж ведуться в гривні.

Принцип: SupplierItem.price_orig зберігає ціну у валюті постачальника, а SupplierItem.cost = price_orig × курс (гривня).
Уся решта системи (каса, правила цін, маржа) читає cost у гривні й про валюти не знає. Коли курс змінюється,
cost усіх позицій постачальника перераховується одним запитом, а автоціна товарів на складі оновлюється.
Курс постачальника = власний курс (якщо задано) або курс НБУ × (1 + запас на курс, %)."""
import json
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Value
from django.db.models.functions import Round
from django.utils import timezone

from .models import Currency, ExchangeRate, Product, Supplier, SupplierItem

NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json&valcode={code}"
CENT, RATE_Q = Decimal("0.01"), Decimal("0.000001")
STALE_DAYS = 3


class RateError(Exception):
    pass


def get_rate(code, on=None):
    """Останній відомий курс валюти на дату `on` (не пізніше неї). UAH = 1."""
    if code == "UAH":
        return Decimal(1)
    on = on or timezone.localdate()
    r = ExchangeRate.objects.filter(currency__code=code, date__lte=on).select_related("currency").first()
    if r is None:
        raise RateError(f"Немає курсу {code}. Задайте його на сторінці «Курси валют» або натисніть «Оновити з НБУ»")
    return r.rate


def is_foreign(sup):
    return bool(sup.currency_id) and not sup.currency.is_base


def effective_rate(sup, on=None):
    if not is_foreign(sup):
        return Decimal(1)
    if sup.fixed_rate:
        return sup.fixed_rate.quantize(RATE_Q)
    return (get_rate(sup.currency.code, on) * (Decimal(100) + sup.rate_margin_pct) / Decimal(100)).quantize(RATE_Q)


def to_uah(amount, rate):
    return (Decimal(amount) * Decimal(rate)).quantize(CENT, ROUND_HALF_UP)


def invoice_rate(sup, code, on=None):
    """Курс для накладної в валюті `code`: умови постачальника, якщо це його валюта, інакше курс НБУ."""
    if not code or code == "UAH":
        return Decimal(1)
    if is_foreign(sup) and sup.currency.code == code:
        return effective_rate(sup, on)
    return get_rate(code, on)


def latest_rates():
    """Для сторінки й індикатора: {код: ExchangeRate} останній курс кожної валюти."""
    out = {}
    for r in ExchangeRate.objects.select_related("currency").order_by("-date", "-pk"):
        out.setdefault(r.currency.code, r)
    return out


def is_stale(r):
    return r is None or (timezone.localdate() - r.date).days > STALE_DAYS


# ---------------------------------------------------------------- НБУ
def fetch_nbu(day=None, fetch=None):
    """Курси НБУ для всіх небазових валют із довідника. Повертає [(код, курс, дата)]."""
    from . import apisync
    fetch = fetch or apisync.fetch_bytes
    out = []
    for cur in Currency.objects.filter(is_base=False):
        url = NBU_URL.format(code=cur.code) + (f"&date={day:%Y%m%d}" if day else "")
        data = json.loads(fetch(url, {"Accept": "application/json", "User-Agent": "RetailShop/1.0"}).decode("utf-8-sig"))
        item = next((x for x in data if str(x.get("cc", cur.code)).upper() == cur.code and x.get("rate")), None) if isinstance(data, list) else None
        if item is None:
            continue
        try:
            when = datetime.strptime(str(item.get("exchangedate", "")), "%d.%m.%Y").date()
        except ValueError:
            when = day or timezone.localdate()
        out.append((cur.code, Decimal(str(item["rate"])).quantize(RATE_Q), when))
    return out


@transaction.atomic
def save_rate(code, rate, day, source="manual", user=None):
    """Зберігає курс на дату. Якщо він змінився, перераховує прайси постачальників у цій валюті й автоціну. Повертає (змінився?, перераховано позицій)."""
    cur = Currency.objects.get(code=code, is_base=False)
    rate = Decimal(rate).quantize(RATE_Q)
    if rate <= 0:
        raise RateError("Курс має бути більшим за 0")
    prev = ExchangeRate.objects.filter(currency=cur, date__lte=day).first()
    obj, created = ExchangeRate.objects.update_or_create(currency=cur, date=day, defaults={"rate": rate, "source": source})
    changed = created or (prev is None) or prev.rate != rate
    n = 0
    if changed and day >= timezone.localdate() - timedelta(days=1):         # курс на «сьогодні» (не історичний запис)
        n = sum(recompute_supplier_costs(s, user)[0] for s in Supplier.objects.filter(currency=cur, fixed_rate__isnull=True))
    return changed, n


def refresh_from_nbu(user=None, fetch=None):
    from .apisync import SyncError
    try:
        rows = fetch_nbu(fetch=fetch)
    except (ValueError, SyncError) as e:
        raise RateError(f"Не вдалося отримати курси НБУ: {e}")
    if not rows:
        raise RateError("НБУ не повернув курсів (можливо, вихідний день). Спробуйте пізніше або внесіть курс вручну")
    parts, total = [], 0
    for code, rate, when in rows:
        _, n = save_rate(code, rate, when, "nbu", user)
        parts.append(f"{code} {rate.normalize():f}")
        total += n
    return ", ".join(parts) + (f". Перераховано позицій у прайсах: {total}" if total else "")


# ---------------------------------------------------------------- перерахунок прайсів
def recompute_supplier_costs(sup, user=None):
    """cost (грн) = price_orig × поточний курс постачальника для всіх його позицій у валюті. Повертає (позицій, товарів з автоціною перераховано)."""
    if not is_foreign(sup):
        return 0, 0
    rate = effective_rate(sup)
    expr = Round(ExpressionWrapper(F("price_orig") * Value(rate), output_field=DecimalField(max_digits=18, decimal_places=6)), 2)
    n = SupplierItem.objects.filter(supplier=sup, price_orig__isnull=False).exclude(cost=expr).update(cost=expr)
    repriced = 0
    if n:
        from . import pricing
        autos = [p for p in Product.objects.filter(auto_price=True) if p.sku_norm]
        if autos:
            have = set(SupplierItem.objects.filter(supplier=sup, sku_norm__in={p.sku_norm for p in autos}).values_list("brand", "sku_norm"))
            keys = {(p.brand.upper(), p.sku_norm) for p in autos} & {(b.upper(), s) for b, s in have}
            repriced = pricing.reprice_after_supplier_change(keys, user)
    return n, repriced
