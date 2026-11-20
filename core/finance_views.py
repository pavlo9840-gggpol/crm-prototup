"""Management finance journal: operational income and expenses."""
from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, F, Sum, ExpressionWrapper, DecimalField, Q
from django.shortcuts import redirect, render
from django.http import HttpResponse
from django.utils import timezone

from .models import BankTransaction, CashOperation, FinanceEntry, Sale, SaleItem, SaleReturnItem
from . import cash as cashlib
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
    cash_out = - (cash_operations.filter(kind="out").aggregate(total=Sum("amount"))["total"] or Decimal("0.00"))
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
            row["cash_out"] += abs(op.amount)
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



@tenant_staff_only
@company_required
def finance_overview(request):
    """Consolidated management P&L; cash/bank movements are deliberately shown separately."""
    today = timezone.localdate()
    first = today.replace(day=1)
    date_from = _date_param(request.GET.get("from"), first)
    date_to = _date_param(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = first, today

    zero = Decimal("0.00")
    decimal_field = DecimalField(max_digits=18, decimal_places=2)
    # Sales are recognized when the goods are actually handed to the customer
    # (earned_at). Returns are recognized on the date the return is processed,
    # not retroactively in the original sale period.
    items = SaleItem.objects.filter(
        company=request.company,
        sale__company=request.company,
        sale__status=Sale.DONE,
        earned_at__date__gte=date_from,
        earned_at__date__lte=date_to,
    )
    sales_totals = items.aggregate(
        revenue=Sum(ExpressionWrapper(F("quantity") * F("price"), output_field=decimal_field)),
        cost=Sum(ExpressionWrapper(F("quantity") * F("cost_price"), output_field=decimal_field)),
        units=Sum("quantity"),
    )
    returns = SaleReturnItem.objects.filter(
        company=request.company,
        sale_return__company=request.company,
        sale_return__sale__company=request.company,
        sale_return__sale__status=Sale.DONE,
        sale_return__created_at__date__gte=date_from,
        sale_return__created_at__date__lte=date_to,
        sale_item__company=request.company,
        sale_item__earned_at__isnull=False,
    )
    return_totals = returns.aggregate(
        revenue=Sum(ExpressionWrapper(F("quantity") * F("price"), output_field=decimal_field)),
        cost=Sum(ExpressionWrapper(F("quantity") * F("sale_item__cost_price"), output_field=decimal_field)),
        units=Sum("quantity"),
    )
    gross_sales = sales_totals["revenue"] or zero
    gross_cost = sales_totals["cost"] or zero
    returned_revenue = return_totals["revenue"] or zero
    returned_cost = return_totals["cost"] or zero
    returned_units = return_totals["units"] or zero
    revenue = gross_sales - returned_revenue
    cost = gross_cost - returned_cost
    units = (sales_totals["units"] or zero) - returned_units
    gross_profit = revenue - cost

    entries = FinanceEntry.objects.filter(
        company=request.company,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
        is_void=False,
    )
    entry_totals = entries.values("entry_type").annotate(total=Sum("amount"))
    entry_amounts = {row["entry_type"]: row["total"] or zero for row in entry_totals}
    other_income = entry_amounts.get(FinanceEntry.INCOME, zero)
    expenses = entry_amounts.get(FinanceEntry.EXPENSE, zero)
    operating_result = gross_profit + other_income - expenses

    expense_rows = list(entries.filter(entry_type=FinanceEntry.EXPENSE)
                         .values("category").annotate(total=Sum("amount"), count=Count("pk"))
                         .order_by("-total", "category"))

    # These are statement/drawer movements, not profit. Keep them separate to
    # avoid counting POS takings or settlements twice.
    cash_ops = CashOperation.objects.filter(
        company=request.company, created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    cash_in = cash_ops.filter(kind="in").aggregate(total=Sum("amount"))["total"] or zero
    cash_out = -(cash_ops.filter(kind="out").aggregate(total=Sum("amount"))["total"] or zero)
    bank_ops = BankTransaction.objects.filter(
        company=request.company, transaction_date__gte=date_from,
        transaction_date__lte=date_to, is_void=False,
    )
    bank_in = bank_ops.filter(direction=BankTransaction.INCOME).aggregate(total=Sum("amount"))["total"] or zero
    bank_out = bank_ops.filter(direction=BankTransaction.EXPENSE).aggregate(total=Sum("amount"))["total"] or zero

    if request.GET.get("export") == "csv":
        import csv
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="financial-overview.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(["Показник", "Сума, грн"])
        rows = [
            ("Продажі за переданими товарами/послугами", gross_sales),
            ("Повернення покупців", returned_revenue),
            ("Чиста виручка", revenue),
            ("Собівартість продажів", gross_cost),
            ("Повернена собівартість", returned_cost),
            ("Собівартість після повернень", cost),
            ("Валова маржа", gross_profit),
            ("Інші доходи", other_income),
            ("Операційні витрати", expenses),
            ("Операційний результат", operating_result),
            ("Каса: внесення", cash_in), ("Каса: вилучення", cash_out),
            ("Банк: надходження", bank_in), ("Банк: списання", bank_out),
        ]
        for label, amount in rows:
            writer.writerow([label, str(amount)])
        writer.writerow([])
        writer.writerow(["Категорія витрат", "Сума, грн", "Кількість записів"])
        for row in expense_rows:
            category = str(row["category"] or "")
            if category[:1] in ("=", "+", "-", "@", "\t", "\r"):
                category = "'" + category
            writer.writerow([category, str(row["total"] or zero), row["count"]])
        return response

    return render(request, "core/finance_overview.html", {
        "date_from": date_from, "date_to": date_to,
        "gross_sales": gross_sales, "returned_revenue": returned_revenue,
        "gross_cost": gross_cost, "returned_cost": returned_cost,
        "returned_units": returned_units, "revenue": revenue, "cost": cost, "gross_profit": gross_profit,
        "other_income": other_income, "expenses": expenses,
        "operating_result": operating_result, "expense_rows": expense_rows,
        "units": units,
        "cash_in": cash_in, "cash_out": cash_out,
        "bank_in": bank_in, "bank_out": bank_out,
    })


def _csv_safe(value):
    """Prevent spreadsheet formula execution in user-controlled CSV cells."""
    text = str(value or "")
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@tenant_staff_only
@company_required
def finance_controls(request):
    """One-screen control report for receivables, advances, bank matching and reversals."""
    from .models import (
        AccountAdvance, BankReconciliation, Customer, Purchase, Sale,
        SettlementPayment, Supplier, SupplierCompensation, Shift,
    )
    from .settlement_views import _balance_for, _supplier_net_balance_for

    today = timezone.localdate()
    first = today.replace(day=1)
    date_from = _date_param(request.GET.get("from"), first)
    date_to = _date_param(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = first, today
    zero = Decimal("0.00")

    # Bank matching is deliberately one-to-one and exact-amount. The report
    # exposes unmatched records; it never silently creates or changes matches.
    bank_rows = BankTransaction.objects.filter(
        company=request.company, is_void=False,
        transaction_date__gte=date_from, transaction_date__lte=date_to,
    ).exclude(reconciliations__is_void=False).exclude(
        supplier_compensation__isnull=False,
    ).exclude(
        supplier_compensation_reversal__isnull=False,
    ).select_related("account").order_by("-transaction_date", "-pk")
    bank_unmatched_in = bank_rows.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or zero
    bank_unmatched_out = bank_rows.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or zero

    payment_rows = SettlementPayment.objects.filter(
        company=request.company, payment_method="bank", is_void=False,
        created_at__date__gte=date_from, created_at__date__lte=date_to,
    ).exclude(bank_reconciliations__is_void=False).select_related(
        "sale__customer", "purchase__supplier", "created_by",
    ).order_by("-created_at", "-pk")
    payment_unmatched_in = payment_rows.filter(sale__isnull=False).aggregate(v=Sum("amount"))["v"] or zero
    payment_unmatched_out = payment_rows.filter(purchase__isnull=False).aggregate(v=Sum("amount"))["v"] or zero

    advances = AccountAdvance.objects.filter(
        company=request.company, is_void=False, remaining__gt=0,
    ).select_related("customer", "supplier").order_by("-remaining", "-created_at")
    customer_advances = advances.filter(party_type=AccountAdvance.CUSTOMER)
    supplier_advances = advances.filter(party_type=AccountAdvance.SUPPLIER)
    customer_advance_total = customer_advances.aggregate(v=Sum("remaining"))["v"] or zero
    supplier_advance_total = supplier_advances.aggregate(v=Sum("remaining"))["v"] or zero

    # Reuse the exact same balance calculation as the account settlement screens.
    # This keeps supplier credit transfers, compensations and customer returns consistent.
    customer_debts = []
    customer_debt_total = zero
    customer_credits = []
    customer_credit_total = zero
    sales = Sale.objects.filter(
        company=request.company, status=Sale.DONE, customer__isnull=False,
    ).select_related("customer").prefetch_related("settlement_payments", "returns")
    for sale in sales:
        balance = _balance_for(sale)
        if balance > 0:
            customer_debt_total += balance
            customer_debts.append({"document": sale, "party": sale.customer, "balance": balance})
        sale_returns = sale._prefetched_objects_cache.get("returns", [])
        returned_total = sum((item.total for item in sale_returns), zero)
        refunded_total = sum((item.cash_amount + item.card_amount for item in sale_returns), zero)
        paid_total = sale.cash_amount + sale.card_amount + sum(
            (payment.amount for payment in sale._prefetched_objects_cache.get("settlement_payments", [])
             if not payment.is_void), zero
        )
        customer_credit = paid_total - refunded_total - (sale.total - returned_total)
        if customer_credit > 0:
            customer_credit_total += customer_credit
            customer_credits.append({"document": sale, "party": sale.customer, "balance": customer_credit})

    supplier_debts = []
    supplier_credits = []
    supplier_debt_total = zero
    supplier_credit_total = zero
    purchases = Purchase.objects.filter(company=request.company).select_related("supplier").prefetch_related(
        "settlement_payments", "supplier_compensations", "credit_applications_in", "credit_applications_out",
    )
    for purchase in purchases:
        balance = _balance_for(purchase)
        if balance > 0:
            supplier_debt_total += balance
            supplier_debts.append({"document": purchase, "party": purchase.supplier, "balance": balance})
        raw_balance = _supplier_net_balance_for(purchase)
        if raw_balance < 0:
            credit = -raw_balance
            supplier_credit_total += credit
            supplier_credits.append({"document": purchase, "party": purchase.supplier, "balance": credit})

    # A voided cash settlement must have its compensating movement recorded.
    reversal_gaps = SettlementPayment.objects.filter(
        company=request.company, is_void=True, cash_operation__isnull=False,
        reversal_operation__isnull=True,
    ).select_related("sale", "purchase", "voided_by").order_by("-voided_at")[:100]

    compensation_reversal_gaps = list(SupplierCompensation.objects.filter(
        company=request.company, is_void=True,
        cash_operation__isnull=False, reversal_cash_operation__isnull=True,
    ).select_related("supplier", "voided_by")[:100])
    compensation_reversal_gaps += list(SupplierCompensation.objects.filter(
        company=request.company, is_void=True,
        bank_transaction__isnull=False, reversal_bank_transaction__isnull=True,
    ).select_related("supplier", "voided_by")[:100])

    # Period closing controls: a cash drawer has a real opening and counted
    # closing only per shift. Bank accounts have no opening-balance field yet,
    # so show period movement rather than inventing an absolute bank balance.
    shifts = Shift.objects.filter(
        company=request.company, opened_at__date__lte=date_to,
    ).filter(
        Q(closed_at__isnull=True) | Q(closed_at__date__gte=date_from)
    ).select_related("cashier").order_by("-opened_at")
    shift_rows = []
    shift_opening_total = zero
    shift_expected_total = zero
    shift_counted_total = zero
    shift_discrepancy_total = zero
    closed_shift_count = 0
    open_shift_count = 0
    for shift in shifts[:250]:
        opening = shift.opening_cash or zero
        expected = Decimal(cashlib.to_dec(cashlib.total(cashlib.clean(shift.drawer))))
        counted = None
        if shift.closed_at and shift.closing_counted is not None:
            counted = Decimal(cashlib.to_dec(cashlib.total(cashlib.clean(shift.closing_counted))))
        shift_opening_total += opening
        shift_expected_total += expected
        if counted is not None:
            shift_counted_total += counted
            shift_discrepancy_total += shift.discrepancy or zero
            closed_shift_count += 1
        else:
            open_shift_count += 1
        shift_rows.append({
            "shift": shift, "opening": opening, "expected": expected,
            "counted": counted, "discrepancy": shift.discrepancy if counted is not None else None,
        })

    cash_period_ops = CashOperation.objects.filter(
        company=request.company, created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    cash_period_in = cash_period_ops.filter(kind="in").aggregate(v=Sum("amount"))["v"] or zero
    cash_period_out = -(cash_period_ops.filter(kind="out").aggregate(v=Sum("amount"))["v"] or zero)
    cash_adjustments = cash_period_ops.filter(kind="adjust").aggregate(v=Sum("amount"))["v"] or zero
    bank_period_ops = BankTransaction.objects.filter(
        company=request.company, transaction_date__gte=date_from,
        transaction_date__lte=date_to, is_void=False,
    )
    bank_period_in = bank_period_ops.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or zero
    bank_period_out = bank_period_ops.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or zero
    period_cash_net = cash_period_in - cash_period_out + cash_adjustments
    period_bank_net = bank_period_in - bank_period_out

    active_matches = BankReconciliation.objects.filter(
        company=request.company, is_void=False,
        bank_transaction__transaction_date__gte=date_from,
        bank_transaction__transaction_date__lte=date_to,
    ).count()
    if request.GET.get("export") == "csv":
        import csv
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="finance-controls-{date_from.isoformat()}-{date_to.isoformat()}.csv"'
        )
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(["Фінансовий контроль", f"{date_from.isoformat()} — {date_to.isoformat()}"])
        writer.writerow(["Показник", "Сума / значення"])
        summary_rows = [
            ("Борг клієнтів", customer_debt_total),
            ("Борг постачальникам", supplier_debt_total),
            ("Переплати клієнтів / до повернення", customer_credit_total),
            ("Кредит постачальників", supplier_credit_total),
            ("Невикористані аванси клієнтів", customer_advance_total),
            ("Невикористані аванси постачальникам", supplier_advance_total),
            ("Незвірені надходження банку", bank_unmatched_in),
            ("Незвірені списання банку", bank_unmatched_out),
            ("Незвірені платежі клієнтів", payment_unmatched_in),
            ("Незвірені платежі постачальникам", payment_unmatched_out),
            ("Каса: надходження", cash_period_in),
            ("Каса: вилучення", cash_period_out),
            ("Каса: корекції", cash_adjustments),
            ("Чистий рух каси", period_cash_net),
            ("Банк: надходження", bank_period_in),
            ("Банк: списання", bank_period_out),
            ("Чистий рух банку", period_bank_net),
            ("Сумарний рух коштів", period_cash_net + period_bank_net),
            ("Закрито касових змін", closed_shift_count),
            ("Відкрито касових змін", open_shift_count),
            ("Розбіжність закритих касових змін", shift_discrepancy_total),
            ("Активні банківські звірки", active_matches),
            ("Сторнування платежів без компенсації", reversal_gaps.count()),
            ("Сторнування компенсацій без зворотної операції", len(compensation_reversal_gaps)),
        ]
        for label, value in summary_rows:
            writer.writerow([label, str(value)])
        writer.writerow([])
        writer.writerow(["Борг клієнта", "Документ", "Сума"])
        for row in sorted(customer_debts, key=lambda item: item["balance"], reverse=True):
            writer.writerow([_csv_safe(row["party"]), _csv_safe(row["document"].number or row["document"].pk), str(row["balance"])])
        writer.writerow([])
        writer.writerow(["Борг постачальнику", "Документ", "Сума"])
        for row in sorted(supplier_debts, key=lambda item: item["balance"], reverse=True):
            writer.writerow([_csv_safe(row["party"]), _csv_safe(row["document"].number or row["document"].pk), str(row["balance"])])
        return response

    return render(request, "core/finance_controls.html", {
        "date_from": date_from, "date_to": date_to,
        "bank_rows": bank_rows[:100], "payment_rows": payment_rows[:100],
        "bank_unmatched_in": bank_unmatched_in, "bank_unmatched_out": bank_unmatched_out,
        "payment_unmatched_in": payment_unmatched_in, "payment_unmatched_out": payment_unmatched_out,
        "customer_advances": customer_advances[:100], "supplier_advances": supplier_advances[:100],
        "customer_advance_total": customer_advance_total, "supplier_advance_total": supplier_advance_total,
        "customer_debts": sorted(customer_debts, key=lambda row: row["balance"], reverse=True)[:100],
        "supplier_debts": sorted(supplier_debts, key=lambda row: row["balance"], reverse=True)[:100],
        "customer_debt_total": customer_debt_total, "supplier_debt_total": supplier_debt_total,
        "customer_credits": sorted(customer_credits, key=lambda row: row["balance"], reverse=True)[:100],
        "customer_credit_total": customer_credit_total,
        "supplier_credits": sorted(supplier_credits, key=lambda row: row["balance"], reverse=True)[:100],
        "supplier_credit_total": supplier_credit_total,
        "reversal_gaps": reversal_gaps,
        "compensation_reversal_gaps": compensation_reversal_gaps,
        "active_matches": active_matches,
        "shift_rows": shift_rows, "closed_shift_count": closed_shift_count,
        "open_shift_count": open_shift_count,
        "shift_opening_total": shift_opening_total,
        "shift_expected_total": shift_expected_total,
        "shift_counted_total": shift_counted_total,
        "shift_discrepancy_total": shift_discrepancy_total,
        "cash_period_in": cash_period_in, "cash_period_out": cash_period_out,
        "cash_adjustments": cash_adjustments, "period_cash_net": period_cash_net,
        "bank_period_in": bank_period_in, "bank_period_out": bank_period_out,
        "period_bank_net": period_bank_net,
        "period_net_movement": period_cash_net + period_bank_net,
    })
