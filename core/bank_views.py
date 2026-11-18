"""Manual bank statement register with audit-safe cancellation."""
from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import BankAccount, BankTransaction
from .tenant_context import company_required
from .views import tenant_staff_only


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["bank_name", "iban", "mfo", "holder", "currency", "note", "is_default"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_iban(self):
        iban = self.cleaned_data["iban"].replace(" ", "").upper()
        qs = BankAccount.objects.filter(company=self.company, iban__iexact=iban)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Такий рахунок уже додано.")
        return iban


class BankTransactionForm(forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = ["account", "transaction_date", "direction", "category", "amount",
                  "counterparty", "document_ref", "note"]
        widgets = {"transaction_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = BankAccount.objects.filter(company=company)
        self.fields["account"].empty_label = "Оберіть рахунок"

    def clean_category(self):
        value = self.cleaned_data["category"].strip()
        if not value:
            raise forms.ValidationError("Вкажіть категорію операції.")
        return value


class BankVoidForm(forms.Form):
    reason = forms.CharField(max_length=250, label="Причина скасування")


def _date(value, fallback):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


@tenant_staff_only
@company_required
def bank_ledger(request):
    today = timezone.localdate()
    start = today.replace(day=1)
    date_from = _date(request.GET.get("from"), start)
    date_to = _date(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = start, today

    account_form = BankAccountForm(company=request.company)
    transaction_form = BankTransactionForm(company=request.company)
    if request.method == "POST":
        if request.POST.get("form_kind") == "account":
            account_form = BankAccountForm(request.POST, company=request.company)
            if account_form.is_valid():
                with transaction.atomic():
                    account = account_form.save(commit=False)
                    account.company = request.company
                    if account.is_default:
                        BankAccount.objects.filter(company=request.company).update(is_default=False)
                    account.save()
                messages.success(request, "Банківський рахунок додано.")
                return redirect("bank_ledger")
        else:
            transaction_form = BankTransactionForm(request.POST, company=request.company)
            if transaction_form.is_valid():
                entry = transaction_form.save(commit=False)
                entry.company = request.company
                entry.created_by = request.user
                entry.save()
                messages.success(request, "Банківську операцію додано до журналу.")
                return redirect(f"{request.path}?from={date_from.isoformat()}&to={date_to.isoformat()}")

    accounts = BankAccount.objects.filter(company=request.company)
    entries = BankTransaction.objects.filter(
        company=request.company, transaction_date__gte=date_from, transaction_date__lte=date_to
    ).select_related("account", "created_by", "voided_by")
    active = entries.filter(is_void=False)
    sums = active.values("direction").annotate(total=Sum("amount"))
    totals = {row["direction"]: row["total"] or Decimal("0.00") for row in sums}
    income = totals.get(BankTransaction.INCOME, Decimal("0.00"))
    expense = totals.get(BankTransaction.EXPENSE, Decimal("0.00"))
    balances = []
    for account in accounts:
        rows = BankTransaction.objects.filter(company=request.company, account=account, is_void=False)
        incoming = rows.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        outgoing = rows.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        balances.append({"account": account, "income": incoming, "expense": outgoing, "balance": incoming - outgoing})
    return render(request, "core/bank_ledger.html", {
        "accounts": accounts, "balances": balances, "entries": entries,
        "account_form": account_form, "transaction_form": transaction_form,
        "date_from": date_from, "date_to": date_to,
        "income": income, "expense": expense, "balance": income - expense,
    })


@tenant_staff_only
@company_required
def bank_transaction_void(request, pk):
    if request.method != "POST":
        return redirect("bank_ledger")
    form = BankVoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Вкажіть причину скасування.")
        return redirect("bank_ledger")
    with transaction.atomic():
        entry = BankTransaction.objects.select_for_update().filter(
            pk=pk, company=request.company, is_void=False
        ).first()
        if not entry:
            messages.error(request, "Операцію не знайдено або її вже скасовано.")
            return redirect("bank_ledger")
        entry.is_void = True
        entry.voided_at = timezone.now()
        entry.voided_by = request.user
        entry.void_reason = form.cleaned_data["reason"].strip()
        entry.save(update_fields=["is_void", "voided_at", "voided_by", "void_reason"])
    messages.success(request, "Операцію скасовано; запис збережено для аудиту.")
    return redirect("bank_ledger")
