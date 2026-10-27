"""Правила цін, перегляд цін («де маржа просіла») і журнал змін цін постачальників."""
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from . import pricing
from .models import PriceRule, Product, SupplierPriceChange
from .views import simple_form, staff_only


class PriceRuleForm(forms.ModelForm):
    class Meta:
        model = PriceRule
        fields = ["name", "brand", "category", "markup_pct", "min_margin_pct", "rounding", "priority", "is_active"]

    def clean_brand(self):
        return " ".join(self.cleaned_data["brand"].split()).upper()

    def clean_min_margin_pct(self):
        v = self.cleaned_data["min_margin_pct"]
        if v is not None and not 0 <= v < 100:
            raise forms.ValidationError("Мінімальна маржа має бути від 0 до 99,99%")
        return v

    def clean_markup_pct(self):
        v = self.cleaned_data["markup_pct"]
        if v is not None and v < 0:
            raise forms.ValidationError("Націнка не може бути від'ємною")
        return v


@staff_only
def price_rules(request):
    rs = pricing.RuleSet()
    rows = []
    for r in PriceRule.objects.select_related("category"):
        ex = pricing.compute_price(1000, pricing.base_pct(None, r), r)
        rows.append({"r": r, "example": ex})
    return render(request, "core/price_rules.html", {"rows": rows, "has_default": any(not r.brand and not r.category_id and r.is_active for r in rs.rules)})


@staff_only
def price_rule_edit(request, pk=None):
    return simple_form(request, PriceRuleForm, "Правило ціни", "price_rules", pk, PriceRule)


def build_rows(brand_q="", only_ids=None):
    """Для кожного товару: що рекомендують правила й чи просіла маржа."""
    qs = Product.objects.filter(is_active=True, cost_price__gt=0).exclude(from_supplier=True, current_stock__lte=0, auto_price=False)
    if only_ids is not None:
        qs = qs.filter(pk__in=only_ids)
    if brand_q:
        qs = qs.filter(Q(brand__icontains=brand_q) | Q(sku__icontains=brand_q))
    prods = list(qs.select_related("category")[:3000])
    rs, repl = pricing.RuleSet(), pricing.replacement_costs(prods)
    rows = []
    for p in prods:
        rule = rs.for_(p.brand, p.category_id)
        r = repl.get((p.brand.upper(), p.sku_norm))
        ref = r or p.cost_price
        rec = pricing.compute_price(ref, pricing.base_pct(None, rule), rule)
        cur_m = pricing.margin_pct(p.price, p.cost_price)
        floor = rule.min_margin_pct if rule else Decimal(0)
        low = bool(floor and cur_m < floor - Decimal("0.01"))
        below = rec - p.price > Decimal("0.01")
        cost_up = bool(r and r > p.cost_price * Decimal("1.005"))
        rows.append({"p": p, "rule": rule, "ref": ref, "repl": r, "rec": rec, "cur_margin": cur_m, "new_margin": pricing.margin_pct(rec, ref),
                     "low": low, "below": below, "cost_up": cost_up, "above": p.price - rec > Decimal("0.01"), "needs": below or low})
    rows.sort(key=lambda x: (not x["needs"], x["p"].price - x["rec"]))
    return rows


@staff_only
def price_review(request):
    brand_q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "problems")
    if request.method == "POST":
        act = request.POST.get("act")
        ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
        if act == "apply_all":
            ids = [r["p"].pk for r in build_rows(request.POST.get("q", "").strip()) if r["needs"]]
        if not ids:
            messages.error(request, "Нічого не вибрано")
        elif act in ("apply", "apply_all"):
            rs, n = pricing.RuleSet(), 0
            for r in build_rows(only_ids=ids):
                if pricing.reprice_product(r["p"], request.user, cost=r["ref"], rs=rs):
                    n += 1
            messages.success(request, f"Ціни перераховано за правилами: {n}")
        elif act in ("enable_auto", "disable_auto"):
            Product.objects.filter(pk__in=ids).update(auto_price=(act == "enable_auto"))
            messages.success(request, ("Автоціну ввімкнено" if act == "enable_auto" else "Автоціну вимкнено") + f" для {len(ids)} товарів")
        return redirect(f"{request.path}?q={brand_q}&show={show}")
    rows = build_rows(brand_q)
    totals = {"all": len(rows), "needs": sum(1 for r in rows if r["needs"]), "low": sum(1 for r in rows if r["low"]),
              "cost_up": sum(1 for r in rows if r["cost_up"]), "auto": sum(1 for r in rows if r["p"].auto_price)}
    shown = {"problems": [r for r in rows if r["needs"]], "auto": [r for r in rows if r["p"].auto_price],
             "all": rows}.get(show, [r for r in rows if r["needs"]])[:400]
    since = timezone.now() - timedelta(days=14)
    changes = []
    for c in SupplierPriceChange.objects.filter(at__gte=since).select_related("item__supplier")[:60]:
        mine = Product.objects.filter(sku_norm=c.item.sku_norm, brand__iexact=c.item.brand).first()
        pct = (c.new_cost - c.old_cost) / c.old_cost * 100 if c.old_cost else Decimal(0)
        changes.append({"c": c, "pct": pct, "mine": mine})
    changes.sort(key=lambda x: (x["mine"] is None, -abs(x["pct"])))
    return render(request, "core/price_review.html", {"rows": shown, "totals": totals, "q": brand_q, "show": show, "changes": changes,
                                                      "rules_count": PriceRule.objects.filter(is_active=True).count()})
