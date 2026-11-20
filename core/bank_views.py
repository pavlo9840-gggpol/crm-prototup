"""Manual bank statement register with audit-safe cancellation."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import AccountAdvance, BankAccount, BankTransaction, BankReconciliation, SettlementPayment, SupplierCompensation
from .tenant_context import company_required
from .views import tenant_staff_only


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["bank_name", "iban", "mfo", "holder", "currency", "opening_balance", "opening_balance_date", "note", "is_default"]
        widgets = {"opening_balance_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if not self.instance.pk and not self.initial.get("opening_balance_date"):
            self.initial["opening_balance_date"] = timezone.localdate()

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
        rows = BankTransaction.objects.filter(
            company=request.company, account=account, is_void=False,
            transaction_date__gte=account.opening_balance_date,
        )
        incoming = rows.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        outgoing = rows.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        period_rows = rows.filter(transaction_date__gte=date_from, transaction_date__lte=date_to)
        period_income = period_rows.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        period_expense = period_rows.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        if date_from >= account.opening_balance_date:
            before_period = rows.filter(transaction_date__lt=date_from)
            before_income = before_period.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
            before_expense = before_period.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
            opening_at_period = account.opening_balance + before_income - before_expense
        else:
            opening_at_period = None
        if date_to >= account.opening_balance_date:
            closing_at_period = account.opening_balance + incoming - outgoing
        else:
            closing_at_period = None
        balances.append({
            "account": account, "income": incoming, "expense": outgoing, "balance": incoming - outgoing,
            "period_income": period_income, "period_expense": period_expense,
            "opening_at_period": opening_at_period, "closing_at_period": closing_at_period,
        })
    active_reconciliations = BankReconciliation.objects.filter(company=request.company, is_void=False).select_related("settlement_payment", "settlement_payment__sale", "settlement_payment__purchase", "account_advance__customer", "account_advance__supplier", "matched_by")
    reconciliation_by_transaction = {r.bank_transaction_id: r for r in active_reconciliations}
    eligible_payments = SettlementPayment.objects.filter(company=request.company, payment_method="bank", is_void=False).exclude(
        bank_reconciliations__is_void=False
    ).select_related("sale", "purchase")
    eligible_advances = AccountAdvance.objects.filter(
        company=request.company, payment_method="bank", is_void=False,
    ).exclude(bank_reconciliations__is_void=False).select_related("customer", "supplier")
    for entry in entries:
        candidates = []
        if not entry.is_void and entry.pk not in reconciliation_by_transaction:
            expected_document = "sale" if entry.direction == BankTransaction.INCOME else "purchase"
            for payment in eligible_payments:
                if payment.amount != entry.amount:
                    continue
                if (expected_document == "sale" and not payment.sale_id) or (expected_document == "purchase" and not payment.purchase_id):
                    continue
                document = payment.sale or payment.purchase
                candidates.append({
                    "ref": f"payment:{payment.pk}",
                    "label": f"{'Продаж' if payment.sale_id else 'Закупівля'} №{document.number or document.pk} · {payment.amount:.2f} ₴ · {payment.created_at:%d.%m.%Y}",
                })
            expected_party = AccountAdvance.CUSTOMER if entry.direction == BankTransaction.INCOME else AccountAdvance.SUPPLIER
            for advance in eligible_advances:
                if advance.party_type != expected_party or advance.amount != entry.amount:
                    continue
                party = advance.customer if advance.party_type == AccountAdvance.CUSTOMER else advance.supplier
                candidates.append({
                    "ref": f"advance:{advance.pk}",
                    "label": f"{'Аванс клієнта' if advance.party_type == AccountAdvance.CUSTOMER else 'Аванс постачальнику'} · {party} · {advance.amount:.2f} ₴ · {advance.created_at:%d.%m.%Y}",
                })
        entry.active_reconciliation = reconciliation_by_transaction.get(entry.pk)
        entry.reconciliation_candidates = candidates
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
        if BankReconciliation.objects.filter(bank_transaction=entry, is_void=False).exists():
            messages.error(request, "Спочатку скасуйте звірку, потім можна скасувати банківський запис.")
            return redirect("bank_ledger")
        if (SupplierCompensation.objects.filter(bank_transaction=entry).exists()
                or SupplierCompensation.objects.filter(reversal_bank_transaction=entry).exists()):
            messages.error(request, "Ця банківська операція належить компенсації постачальника. Скасуйте всю компенсацію через документ повернення.")
            return redirect("bank_ledger")
        entry.is_void = True
        entry.voided_at = timezone.now()
        entry.voided_by = request.user
        entry.void_reason = form.cleaned_data["reason"].strip()
        entry.save(update_fields=["is_void", "voided_at", "voided_by", "void_reason"])
    messages.success(request, "Операцію скасовано; запис збережено для аудиту.")
    return redirect("bank_ledger")

import csv
import hashlib
import io
from datetime import datetime


class BankImportForm(forms.Form):
    account = forms.ModelChoiceField(queryset=BankAccount.objects.none(), label="Рахунок")
    file = forms.FileField(label="CSV-файл виписки")

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = BankAccount.objects.filter(company=company)

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Файл має бути не більше 5 МБ.")
        if not upload.name.lower().endswith(".csv"):
            raise forms.ValidationError("Поки підтримується лише CSV.")
        return upload


def _normalize_header(value):
    return "".join(ch for ch in (value or "").strip().lower() if ch.isalnum())


def _pick(row, *aliases):
    normalized = {_normalize_header(k): (v or "").strip() for k, v in row.items()}
    for alias in aliases:
        if _normalize_header(alias) in normalized:
            return normalized[_normalize_header(alias)]
    return ""


def _parse_import_date(value):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("Невідомий формат дати")


def _parse_import_amount(value):
    raw = (value or "").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    raw = raw.replace("₴", "").replace("UAH", "").replace("uah", "")
    amount = Decimal(raw)
    if amount <= 0:
        raise ValueError("Сума має бути більшою за нуль")
    return amount.quantize(Decimal("0.01"))


def _import_fingerprint(account_id, transaction_date, direction, amount, counterparty, document_ref):
    raw = "|".join([
        str(account_id), transaction_date.isoformat(), direction, format(amount, ".2f"),
        (counterparty or "").strip().casefold(), (document_ref or "").strip().casefold(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@tenant_staff_only
@company_required
def bank_import(request):
    form = BankImportForm(request.POST or None, request.FILES or None, company=request.company)
    imported = duplicate_count = error_count = 0
    errors = []
    if request.method == "POST" and form.is_valid():
        account = form.cleaned_data["account"]
        try:
            content = form.cleaned_data["file"].read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content))
            if not reader.fieldnames:
                raise ValueError("CSV не містить заголовків.")
            with transaction.atomic():
                for row_no, row in enumerate(reader, start=2):
                    try:
                        date_value = _pick(row, "date", "transaction_date", "Дата", "Дата операції")
                        direction_value = _pick(row, "direction", "type", "Напрямок", "Тип")
                        amount_value = _pick(row, "amount", "sum", "Сума", "Сума операції")
                        iban_value = _pick(row, "iban", "account", "Рахунок", "IBAN")
                        if iban_value and iban_value.replace(" ", "").upper() != account.iban.replace(" ", "").upper():
                            raise ValueError("IBAN рядка не відповідає вибраному рахунку")
                        transaction_date = _parse_import_date(date_value)
                        direction_text = direction_value.strip().lower()
                        if direction_text in ("income", "in", "credit", "надходження", "зарахування", "дохід"):
                            direction = BankTransaction.INCOME
                        elif direction_text in ("expense", "out", "debit", "списання", "витрата", "видаток"):
                            direction = BankTransaction.EXPENSE
                        else:
                            raise ValueError("Невідомий напрямок; використовуйте income або expense")
                        amount = _parse_import_amount(amount_value)
                        counterparty = _pick(row, "counterparty", "payee", "payer", "Контрагент", "Платник", "Отримувач")
                        document_ref = _pick(row, "document_ref", "purpose", "description", "Призначення", "Призначення платежу")
                        category = _pick(row, "category", "Категорія") or "Імпорт виписки"
                        note = _pick(row, "note", "Примітка")
                        fingerprint = _import_fingerprint(account.pk, transaction_date, direction, amount, counterparty, document_ref)
                        if BankTransaction.objects.filter(company=request.company, import_fingerprint=fingerprint).exists():
                            duplicate_count += 1
                            continue
                        try:
                            with transaction.atomic():
                                BankTransaction.objects.create(
                                    company=request.company, account=account, transaction_date=transaction_date,
                                    direction=direction, category=category[:80], amount=amount,
                                    counterparty=counterparty[:150], document_ref=document_ref[:120], note=note[:250],
                                    created_by=request.user, import_fingerprint=fingerprint,
                                )
                            imported += 1
                        except IntegrityError:
                            duplicate_count += 1
                    except (ValueError, InvalidOperation) as exc:
                        error_count += 1
                        errors.append(f"Рядок {row_no}: {exc}")
            messages.success(request, f"Імпорт завершено: додано {imported}, дублікати {duplicate_count}, помилки {error_count}.")
        except (UnicodeDecodeError, csv.Error, ValueError) as exc:
            messages.error(request, f"Не вдалося прочитати CSV: {exc}")
    return render(request, "core/bank_import.html", {
        "form": form, "imported": imported, "duplicate_count": duplicate_count,
        "error_count": error_count, "errors": errors,
    })


@tenant_staff_only
@company_required
def _bank_csv_cell(value):
    text = str(value or "")
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@tenant_staff_only
@company_required
def bank_export_csv(request):
    """Export company-scoped bank journal for the selected period."""
    today = timezone.localdate()
    start = today.replace(day=1)
    date_from = _date(request.GET.get("from"), start)
    date_to = _date(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = start, today
    entries = BankTransaction.objects.filter(
        company=request.company, transaction_date__gte=date_from, transaction_date__lte=date_to
    ).select_related("account").order_by("transaction_date", "id")
    from django.http import HttpResponse
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="bank-journal-%s-%s.csv"' % (date_from.isoformat(), date_to.isoformat())
    response.write("\\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "Дата", "Банк", "IBAN", "Валюта", "Початковий залишок", "Дата початкового залишку",
        "Напрямок", "Категорія", "Сума", "Контрагент", "Документ/призначення", "Примітка",
        "Статус", "Причина скасування", "Звірка", "Документ звірки", "Звірено користувачем",
        "Дата звірки", "Причина скасування звірки",
    ])
    for entry in entries:
        reconciliation = BankReconciliation.objects.filter(
            company=request.company, bank_transaction=entry, is_void=False
        ).select_related(
            "settlement_payment", "settlement_payment__sale", "settlement_payment__purchase",
            "account_advance__customer", "account_advance__supplier", "matched_by",
        ).first()
        if reconciliation:
            if reconciliation.settlement_payment_id:
                payment = reconciliation.settlement_payment
                document = payment.sale or payment.purchase
                match_document = f"{'Продаж' if payment.sale_id else 'Закупівля'} №{document.number or document.pk}"
            else:
                advance = reconciliation.account_advance
                party = advance.customer if advance.party_type == AccountAdvance.CUSTOMER else advance.supplier
                match_document = f"{'Аванс клієнта' if advance.party_type == AccountAdvance.CUSTOMER else 'Аванс постачальнику'} · {party}"
            match_status = "Звірено"
            match_user = reconciliation.matched_by.get_username()
            match_date = reconciliation.matched_at.isoformat()
            match_void_reason = ""
        else:
            match_status, match_document, match_user, match_date, match_void_reason = "Не звірено", "", "", "", ""
        writer.writerow([
            entry.transaction_date.isoformat(), _bank_csv_cell(entry.account.bank_name),
            _bank_csv_cell(entry.account.iban), _bank_csv_cell(entry.account.currency),
            str(entry.account.opening_balance), entry.account.opening_balance_date.isoformat(),
            _bank_csv_cell(entry.get_direction_display()), _bank_csv_cell(entry.category), str(entry.amount),
            _bank_csv_cell(entry.counterparty), _bank_csv_cell(entry.document_ref), _bank_csv_cell(entry.note),
            "Скасовано" if entry.is_void else "Активна", _bank_csv_cell(entry.void_reason),
            match_status, _bank_csv_cell(match_document), _bank_csv_cell(match_user), match_date,
            _bank_csv_cell(match_void_reason),
        ])
    return response

@tenant_staff_only
@company_required
def bank_reconcile(request, pk):
    if request.method != "POST":
        return redirect("bank_ledger")
    with transaction.atomic():
        entry = BankTransaction.objects.select_for_update().filter(pk=pk, company=request.company, is_void=False).first()
        if not entry:
            messages.error(request, "Банківську операцію не знайдено.")
            return redirect("bank_ledger")
        if BankReconciliation.objects.filter(bank_transaction=entry, is_void=False).exists():
            messages.error(request, "Ця операція вже звірена.")
            return redirect("bank_ledger")
        match_ref = (request.POST.get("match_ref") or "").strip()
        try:
            match_type, raw_pk = match_ref.split(":", 1)
            match_pk = int(raw_pk)
        except (ValueError, TypeError):
            messages.error(request, "Оберіть платіж або аванс для звірки.")
            return redirect("bank_ledger")
        if match_type == "payment":
            payment = SettlementPayment.objects.select_for_update().filter(
                pk=match_pk, company=request.company, payment_method="bank", is_void=False,
            ).select_related("sale", "purchase").first()
            if not payment:
                messages.error(request, "Платіж не знайдено або він недоступний для звірки.")
                return redirect("bank_ledger")
            expected_direction = BankTransaction.INCOME if payment.sale_id else BankTransaction.EXPENSE
            if payment.amount != entry.amount or expected_direction != entry.direction:
                messages.error(request, "Сума або напрямок платежу не відповідає банківській операції.")
                return redirect("bank_ledger")
            if BankReconciliation.objects.filter(settlement_payment=payment, is_void=False).exists():
                messages.error(request, "Цей платіж уже звірено з іншою банківською операцією.")
                return redirect("bank_ledger")
            match_kwargs = {"settlement_payment": payment}
        elif match_type == "advance":
            advance = AccountAdvance.objects.select_for_update().filter(
                pk=match_pk, company=request.company, payment_method="bank", is_void=False,
            ).select_related("customer", "supplier").first()
            if not advance:
                messages.error(request, "Аванс не знайдено або він недоступний для звірки.")
                return redirect("bank_ledger")
            expected_direction = BankTransaction.INCOME if advance.party_type == AccountAdvance.CUSTOMER else BankTransaction.EXPENSE
            if advance.amount != entry.amount or expected_direction != entry.direction:
                messages.error(request, "Сума або напрямок банківської операції не відповідає авансу.")
                return redirect("bank_ledger")
            if BankReconciliation.objects.filter(account_advance=advance, is_void=False).exists():
                messages.error(request, "Цей аванс уже звірено з іншою банківською операцією.")
                return redirect("bank_ledger")
            match_kwargs = {"account_advance": advance}
        else:
            messages.error(request, "Невідомий тип документа для звірки.")
            return redirect("bank_ledger")
        BankReconciliation.objects.create(
            company=request.company, bank_transaction=entry, matched_by=request.user, **match_kwargs,
        )
    messages.success(request, "Банківську операцію звірено з документом. Обидва записи залишаються в історії.")
    return redirect("bank_ledger")


@tenant_staff_only
@company_required
def bank_reconciliation_void(request, pk):
    if request.method != "POST":
        return redirect("bank_ledger")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Вкажіть причину скасування звірки.")
        return redirect("bank_ledger")
    with transaction.atomic():
        reconciliation = BankReconciliation.objects.select_for_update().filter(
            pk=pk, company=request.company, is_void=False,
        ).first()
        if not reconciliation:
            messages.error(request, "Звірку не знайдено або вже скасовано.")
            return redirect("bank_ledger")
        reconciliation.is_void = True
        reconciliation.voided_at = timezone.now()
        reconciliation.voided_by = request.user
        reconciliation.void_reason = reason[:250]
        reconciliation.save(update_fields=["is_void", "voided_at", "voided_by", "void_reason"])
    messages.success(request, "Звірку скасовано; записи залишилися в аудиті.")
    return redirect("bank_ledger")
