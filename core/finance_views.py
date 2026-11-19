"""Management finance journal: operational income and expenses."""
from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import BankTransaction, CashOperation, FinanceEntry
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
    ).select_related("created_by", "voided_by")
    active_entries = entries.filter(is_void=False)
    totals = active_entries.values("entry_type").annotate(total=Sum("amount"))
    amounts = {row["entry_type"]: row["total"] or Decimal("0.00") for row in totals}
    income = amounts.get(FinanceEntry.INCOME, Decimal("0.00"))
    expense = amounts.get(FinanceEntry.EXPENSE, Decimal("0.00"))
    payment_rows = active_entries.values("payment_method", "entry_type").annotate(total=Sum("amount"))
    payment_totals = {}
    for row in payment_rows:
        payment_totals.setdefault(row["payment_method"], {"income": Decimal("0.00"), "expense": Decimal("0.00")})
        payment_totals[row["payment_method"]][row["entry_type"]] = row["total"] or Decimal("0.00")
    for totals_by_method in payment_totals.values():
        totals_by_method["balance"] = totals_by_method["income"] - totals_by_method["expense"]
    return render(request, "core/finance_ledger.html", {
        "form": form, "entries": entries, "date_from": date_from, "date_to": date_to,
        "income": income, "expense": expense, "balance": income - expense,
        "payment_totals": payment_totals,
    })



class FinanceVoidForm(forms.Form):
    reason = forms.CharField(
        max_length=250,
        label="Причина скасування",
        widget=forms.TextInput(attrs={"placeholder": "Наприклад: помилково внесена сума"}),
    )


@tenant_staff_only
@company_required
def finance_entry_void(request, pk):
    if request.method != "POST":
        return redirect("finance_ledger")
    form = FinanceVoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Вкажіть причину скасування (до 250 символів).")
        return redirect("finance_ledger")
    with transaction.atomic():
        entry = FinanceEntry.objects.select_for_update().filter(
            pk=pk, company=request.company, is_void=False
        ).first()
        if not entry:
            messages.error(request, "Операцію не знайдено або її вже скасовано.")
            return redirect("finance_ledger")
        entry.is_void = True
        entry.voided_at = timezone.now()
        entry.voided_by = request.user
        entry.void_reason = form.cleaned_data["reason"].strip()
        entry.save(update_fields=["is_void", "voided_at", "voided_by", "void_reason"])
    messages.success(request, "Операцію скасовано. Запис залишився в журналі для аудиту.")
    return redirect("finance_ledger")

@tenant_staff_only
@company_required
def cashflow_report(request):
    today = timezone.localdate()
    first = today.replace(day=1)
    date_from = _date_param(request.GET.get("from"), first)
    date_to = _date_param(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = first, today

    cash_operations = CashOperation.objects.filter(
        company=request.company, created_at__date__gte=date_from, created_at__date__lte=date_to
    ).select_related("shift", "user")
    bank_operations = BankTransaction.objects.filter(
        company=request.company, transaction_date__gte=date_from, transaction_date__lte=date_to, is_void=False
    ).select_related("account", "created_by")

    cash_in = cash_operations.filter(kind="in").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    cash_out = cash_operations.filter(kind="out").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    cash_adjust = cash_operations.filter(kind="adjust").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    bank_in = bank_operations.filter(direction=BankTransaction.INCOME).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    bank_out = bank_operations.filter(direction=BankTransaction.EXPENSE).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    daily = {}
    for op in cash_operations:
        day = op.created_at.date()
        row = daily.setdefault(day, {"cash_in": Decimal("0.00"), "cash_out": Decimal("0.00"), "cash_adjust": Decimal("0.00"), "bank_in": Decimal("0.00"), "bank_out": Decimal("0.00")})
        if op.kind == "in":
            row["cash_in"] += op.amount
        elif op.kind == "out":
            row["cash_out"] += op.amount
        else:
            row["cash_adjust"] += op.amount
    for op in bank_operations:
        day = op.transaction_date
        row = daily.setdefault(day, {"cash_in": Decimal("0.00"), "cash_out": Decimal("0.00"), "cash_adjust": Decimal("0.00"), "bank_in": Decimal("0.00"), "bank_out": Decimal("0.00")})
        if op.direction == BankTransaction.INCOME:
            row["bank_in"] += op.amount
        else:
            row["bank_out"] += op.amount

    daily_rows = []
    for day in sorted(daily, reverse=True):
        row = daily[day]
        row["date"] = day
        row["cash_net"] = row["cash_in"] - row["cash_out"]
        row["bank_net"] = row["bank_in"] - row["bank_out"]
        row["net"] = row["cash_net"] + row["bank_net"]
        daily_rows.append(row)

    return render(request, "core/cashflow_report.html", {
        "date_from": date_from, "date_to": date_to,
        "cash_operations": cash_operations, "bank_operations": bank_operations,
        "daily_rows": daily_rows,
        "cash_in": cash_in, "cash_out": cash_out, "cash_adjust": cash_adjust,
        "bank_in": bank_in, "bank_out": bank_out,
        "cash_net": cash_in - cash_out, "bank_net": bank_in - bank_out,
        "total_in": cash_in + bank_in, "total_out": cash_out + bank_out,
        "total_net": cash_in - cash_out + bank_in - bank_out,
    })
