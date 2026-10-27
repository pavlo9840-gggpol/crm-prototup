"""Відомість зарплати менеджерів і плани оплати."""
import csv
from datetime import date

from django import forms
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from . import payroll
from .models import PayPlan
from .views import get_date, simple_form, staff_only


class PayPlanForm(forms.ModelForm):
    class Meta:
        model = PayPlan
        fields = ["user", "margin_pct", "fixed_monthly", "note"]

    def clean_margin_pct(self):
        v = self.cleaned_data["margin_pct"]
        if not 0 <= v <= 100:
            raise forms.ValidationError("Відсоток від 0 до 100")
        return v

    def clean_fixed_monthly(self):
        v = self.cleaned_data["fixed_monthly"]
        if v < 0:
            raise forms.ValidationError("Сума не може бути від'ємною")
        return v

    def clean(self):
        d = super().clean()
        if "user" in d and d["user"] is None:                     # типовий план може бути лише один
            dup = PayPlan.objects.filter(user__isnull=True).exclude(pk=self.instance.pk)
            if dup.exists():
                self.add_error("user", "Типовий план уже існує: змініть його або оберіть конкретного менеджера")
        return d


@staff_only
def pay_plans(request):
    plans = list(PayPlan.objects.select_related("user").order_by("user__username"))
    covered = {p.user_id for p in plans}
    no_plan = [u for u in get_user_model().objects.filter(is_active=True) if u.pk not in covered]
    has_default = any(p.user_id is None for p in plans)
    return render(request, "core/pay_plans.html", {"plans": plans, "no_plan": no_plan if not has_default else [], "has_default": has_default})


@staff_only
def pay_plan_edit(request, pk=None):
    return simple_form(request, PayPlanForm, "План оплати", "pay_plans", pk, PayPlan)


@staff_only
def payroll_view(request):
    today = timezone.localdate()
    d1, d2 = get_date(request, "from", today.replace(day=1)), get_date(request, "to", today)
    incl = bool(request.GET.get("pending"))
    rows, totals = payroll.compute(d1, d2, include_pending=incl)
    if request.GET.get("csv"):
        resp = HttpResponse(content_type="text/csv; charset=utf-8-sig")
        resp["Content-Disposition"] = f'attachment; filename="payroll_{d1}_{d2}.csv"'
        w = csv.writer(resp, delimiter=";")
        w.writerow(["Менеджер", "Чеків", "Продано", "Маржа", "Маржа %", "В дорозі", "% від маржі", "Змінна частина", "Фіксована", "До виплати"])
        for r in rows:
            w.writerow([r["user"].username, r["sales"], r["revenue"], r["margin"], f"{r['margin_pct']:.1f}", r["pending"], r["pct"],
                        r["variable"], r["fixed"], r["total"]])
        return resp
    return render(request, "core/payroll.html", {"rows": rows, "totals": totals, "d1": d1, "d2": d2, "incl": incl, "frac": payroll.months_fraction(d1, d2)})
