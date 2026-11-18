"""Management finance journal: operational income and expenses."""
from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import FinanceEntry
from .tenant_context import company_required
from .views import tenant_staff_only


class FinanceEntryForm(forms.ModelForm):
    class Meta:
        model = FinanceEntry
        fields = ["entry_type", "category", "amount", "payment_method", "description", "document_ref"]
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "Наприклад: оренда приміщення"}),
            "document_ref": forms.TextInput(attrs={"placeholder": "За наявності"}),
        }

    def clean_category(self):
        value = self.cleaned_data["category"].strip()
        if not value:
            raise forms.ValidationError("Вкажіть категорію операції.")
        return value


def _date_param(value, fallback):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


@tenant_staff_only
@company_required
def finance_ledger(request):
    today = timezone.localdate()
    first = today.replace(day=1)
    date_from = _date_param(request.GET.get("from"), first)
    date_to = _date_param(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = first, today

    if request.method == "POST":
        form = FinanceEntryForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.company = request.company
            entry.created_by = request.user
            entry.save()
            messages.success(request, "Фінансову операцію додано до журналу.")
            return redirect(f"{request.path}?from={date_from.isoformat()}&to={date_to.isoformat()}")
    else:
        form = FinanceEntryForm()

    entries = FinanceEntry.objects.filter(
        company=request.company, created_at__date__gte=date_from, created_at__date__lte=date_to
    ).select_related("created_by")
    totals = entries.values("entry_type").annotate(total=Sum("amount"))
    amounts = {row["entry_type"]: row["total"] or Decimal("0.00") for row in totals}
    income = amounts.get(FinanceEntry.INCOME, Decimal("0.00"))
    expense = amounts.get(FinanceEntry.EXPENSE, Decimal("0.00"))
    return render(request, "core/finance_ledger.html", {
        "form": form, "entries": entries, "date_from": date_from, "date_to": date_to,
        "income": income, "expense": expense, "balance": income - expense,
    })
