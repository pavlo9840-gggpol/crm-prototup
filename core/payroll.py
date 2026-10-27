"""Зарплата менеджерів: відсоток від маржі + фіксована частина.

Маржа вважається зарахованою (earned_at): для товару зі складу в момент чека, для замовленого у постачальника коли клієнт його забрав.
Поки товар в дорозі чи чекає клієнта, його маржа показується окремо як «в дорозі» і в нарахування не входить.
Повернення зменшують маржу. Відʼємна маржа зменшує базу, але змінна частина не буває меншою за 0."""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum

from .models import PayPlan, Sale, SaleItem

D0 = Decimal("0")
DEC = DecimalField(max_digits=16, decimal_places=2)
REV = ExpressionWrapper((F("quantity") - F("returned_qty")) * F("price"), output_field=DEC)
COST = ExpressionWrapper((F("quantity") - F("returned_qty")) * F("cost_price"), output_field=DEC)


def months_fraction(d1, d2):
    """Скільки «місяців» у періоді: повний місяць = 1, частина місяця пропорційно днях."""
    total, cur = D0, d1
    while cur <= d2:
        dim = calendar.monthrange(cur.year, cur.month)[1]
        end = min(d2, date(cur.year, cur.month, dim))
        total += Decimal((end - cur).days + 1) / dim
        cur = end + timedelta(days=1)
    return total


def _agg(qs):
    out = {}
    for r in qs.annotate(l_rev=REV, l_cst=COST).values("sale__cashier_id").annotate(rev=Sum("l_rev"), cst=Sum("l_cst"), n=Count("sale", distinct=True)):
        out[r["sale__cashier_id"]] = {"rev": r["rev"] or D0, "cst": r["cst"] or D0, "n": r["n"]}
    return out


def compute(d1, d2, include_pending=False, only_user=None):
    base = SaleItem.objects.filter(sale__status=Sale.DONE, returned_qty__lt=F("quantity"))
    if only_user is not None:
        base = base.filter(sale__cashier=only_user)
    earned = _agg(base.filter(earned_at__date__gte=d1, earned_at__date__lte=d2))
    pending = _agg(base.filter(earned_at__isnull=True, source=SaleItem.SUPPLIER, fulfil_status__in=[SaleItem.ORDERED, SaleItem.ARRIVED]))
    plans = {p.user_id: p for p in PayPlan.objects.all()}
    default = plans.get(None)
    frac = months_fraction(d1, d2)
    User = get_user_model()
    ids = set(earned) | set(pending) | {u for u in plans if u is not None}
    if default and only_user is None:                  # типовий план (з фіксованою частиною) діє й для тих, хто нічого не продав
        ids |= set(User.objects.filter(is_active=True, is_staff=False).values_list("pk", flat=True))
    if only_user is not None:
        ids = {only_user.pk}
    users = {u.pk: u for u in User.objects.filter(pk__in=ids)}
    rows = []
    for uid in ids:
        u = users.get(uid)
        if u is None or not u.is_active:
            continue
        e, pd = earned.get(uid, {"rev": D0, "cst": D0, "n": 0}), pending.get(uid, {"rev": D0, "cst": D0, "n": 0})
        plan = plans.get(uid) or default
        margin, pend_margin = e["rev"] - e["cst"], pd["rev"] - pd["cst"]
        base_margin = margin + (pend_margin if include_pending else D0)
        pct = plan.margin_pct if plan else D0
        variable = (max(base_margin, D0) * pct / 100).quantize(Decimal("0.01"))
        fixed = ((plan.fixed_monthly if plan else D0) * frac).quantize(Decimal("0.01"))
        rows.append({"user": u, "plan": plan, "own_plan": uid in plans, "sales": e["n"], "revenue": e["rev"], "cost": e["cst"], "margin": margin,
                     "margin_pct": (margin / e["rev"] * 100) if e["rev"] else D0, "pending": pend_margin, "pct": pct,
                     "variable": variable, "fixed": fixed, "total": variable + fixed})
    rows.sort(key=lambda r: (-r["margin"], r["user"].username))
    totals = {k: sum((r[k] for r in rows), D0) for k in ("revenue", "margin", "pending", "variable", "fixed", "total")}
    return rows, totals
