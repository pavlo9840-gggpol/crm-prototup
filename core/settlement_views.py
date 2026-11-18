"""Customer and supplier document settlement register."""
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import cash as cashlib
from .models import CashOperation, Purchase, Sale, SettlementPayment, Shift
from .tenant_context import company_required
from .views import current_shift, tenant_staff_only

D0 = Decimal("0.00")


class SettlementPaymentForm(forms.Form):
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), label="Сума оплати")
    payment_method = forms.ChoiceField(choices=SettlementPayment.PAYMENT_METHODS, label="Спосіб оплати")
    document_ref = forms.CharField(max_length=80, required=False, label="Документ оплати")
    note = forms.CharField(max_length=250, required=False, label="Примітка")


class SettlementVoidForm(forms.Form):
    reason = forms.CharField(max_length=250, label="Причина скасування")


def _paid_for(document):
    cached = getattr(document, "_prefetched_objects_cache", {}).get("settlement_payments")
    if cached is not None:
        return sum((payment.amount for payment in cached if not payment.is_void), D0)
    return document.settlement_payments.filter(company=document.company, is_void=False).aggregate(total=Sum("amount"))["total"] or D0


def _balance_for(document):
    if isinstance(document, Purchase):
        return max(D0, document.total - _paid_for(document))
    cached_returns = getattr(document, "_prefetched_objects_cache", {}).get("returns")
    returned = (sum((item.total for item in cached_returns), D0) if cached_returns is not None
                else document.returns.filter(company=document.company).aggregate(total=Sum("total"))["total"] or D0)
    already_paid = document.cash_amount + document.card_amount + _paid_for(document)
    return max(D0, document.total - returned - already_paid)


def _settlement_cash_plan(document, amount, shift):
    """Return drawer delta/counts for a cash settlement; None means insufficient notes."""
    drawer = cashlib.clean(shift.drawer)
    amount_kop = cashlib.to_kop(amount)
    if isinstance(document, Purchase):
        counts = cashlib.make_change(amount_kop, drawer)
        if counts is None:
            return None
        return cashlib.add(drawer, counts, -1), counts, "out"
    counts = cashlib.decompose(amount_kop)
    return cashlib.add(drawer, counts), counts, "in"


def _reverse_settlement_cash(payment, shift):
    """Create a compensating cash movement; never rewrite/delete the original one."""
    original = payment.cash_operation
    drawer = cashlib.clean(shift.drawer)
    amount_kop = cashlib.to_kop(payment.amount)
    if original.kind == "in":
        counts = cashlib.make_change(amount_kop, drawer)
        if counts is None:
            return None
        drawer_after = cashlib.add(drawer, counts, -1)
        kind, signed_amount = "out", -payment.amount
    else:
        counts = cashlib.clean(original.detail.get("counts", {}))
        drawer_after = cashlib.add(drawer, counts)
        kind, signed_amount = "in", payment.amount
    return drawer_after, counts, kind, signed_amount


@tenant_staff_only
@company_required
def settlements_home(request):
    purchase_docs = Purchase.objects.filter(company=request.company).select_related("supplier").prefetch_related("settlement_payments")
    sale_docs = Sale.objects.filter(company=request.company, status=Sale.DONE, customer__isnull=False).select_related("customer").prefetch_related("settlement_payments", "returns")
    supplier_rows, customer_rows = [], []
    supplier_total = customer_total = D0
    for document in purchase_docs:
        balance = _balance_for(document)
        if balance > 0:
            supplier_total += balance
            supplier_rows.append({"document": document, "balance": balance, "party": document.supplier})
    for document in sale_docs:
        balance = _balance_for(document)
        if balance > 0:
            customer_total += balance
            customer_rows.append({"document": document, "balance": balance, "party": document.customer})
    payments = SettlementPayment.objects.filter(company=request.company).select_related(
        "sale__customer", "purchase__supplier", "created_by", "voided_by",
        "cash_operation__shift", "reversal_operation",
    )[:200]
    return render(request, "core/settlements.html", {
        "supplier_rows": supplier_rows, "customer_rows": customer_rows,
        "supplier_total": supplier_total, "customer_total": customer_total,
        "payments": payments,
    })


@tenant_staff_only
@company_required
def settlement_payment_new(request, document_type, pk):
    if request.method != "POST":
        return redirect("settlements_home")
    if document_type not in {"sale", "purchase"}:
        messages.error(request, "Невідомий тип документа.")
        return redirect("settlements_home")
    with transaction.atomic():
        if document_type == "purchase":
            document = get_object_or_404(Purchase.objects.select_for_update().filter(company=request.company), pk=pk)
        else:
            document = get_object_or_404(Sale.objects.select_for_update().filter(
                company=request.company, status=Sale.DONE, customer__isnull=False
            ), pk=pk)
        form = SettlementPaymentForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Перевірте суму та спосіб оплати.")
            return redirect("purchase_detail" if document_type == "purchase" else "sale_detail", pk=document.pk)
        balance = _balance_for(document)
        amount = form.cleaned_data["amount"]
        payment_method = form.cleaned_data["payment_method"]
        shift = None
        cash_plan = None
        if balance <= 0:
            messages.error(request, "За цим документом немає непогашеної заборгованості.")
        elif amount > balance:
            messages.error(request, f"Сума перевищує залишок боргу: {balance:.2f} ₴.")
        elif payment_method == "cash" and not (shift := Shift.objects.select_for_update().filter(
            company=request.company, cashier=request.user, closed_at__isnull=True
        ).first()):
            messages.error(request, "Для готівкової оплати відкрийте касову зміну під своїм обліковим записом.")
        else:
            if payment_method == "cash":
                cash_plan = _settlement_cash_plan(document, amount, shift)
                if cash_plan is None:
                    messages.error(request, "У касі недостатньо готівки потрібного номіналу для виплати постачальнику.")
            if payment_method != "cash" or cash_plan is not None:
                kwargs = {"purchase": document} if document_type == "purchase" else {"sale": document}
                payment = SettlementPayment.objects.create(
                    company=request.company, created_by=request.user, amount=amount,
                    payment_method=payment_method,
                    document_ref=form.cleaned_data["document_ref"].strip(),
                    note=form.cleaned_data["note"].strip(), **kwargs,
                )
                if cash_plan is not None:
                    drawer_after, counts, kind = cash_plan
                    signed_amount = amount if kind == "in" else -amount
                    operation = CashOperation.objects.create(
                        company=request.company, shift=shift, user=request.user, kind=kind,
                        amount=signed_amount,
                        detail={"source": "settlement", "settlement_payment_id": payment.pk,
                                "counts": cashlib.store(counts)},
                        note=("Погашення боргу клієнта" if kind == "in" else "Оплата постачальнику") +
                             f" · {document}",
                    )
                    payment.cash_operation = operation
                    payment.save(update_fields=["cash_operation"])
                    shift.drawer = cashlib.store(drawer_after)
                    shift.save(update_fields=["drawer"])
                messages.success(request, f"Оплату {amount:.2f} ₴ зареєстровано.")
    return redirect("purchase_detail" if document_type == "purchase" else "sale_detail", pk=document.pk)


@tenant_staff_only
@company_required
def settlement_payment_void(request, pk):
    if request.method != "POST":
        return redirect("settlements_home")
    form = SettlementVoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Вкажіть причину скасування.")
        return redirect("settlements_home")
    with transaction.atomic():
        payment = SettlementPayment.objects.select_for_update().filter(
            pk=pk, company=request.company, is_void=False
        ).select_related("sale", "purchase", "cash_operation").first()
        if not payment:
            messages.error(request, "Оплату не знайдено або її вже скасовано.")
            return redirect("settlements_home")
        if payment.cash_operation_id:
            shift = Shift.objects.select_for_update().filter(
                pk=payment.cash_operation.shift_id, company=request.company, closed_at__isnull=True
            ).first()
            if not shift:
                messages.error(request, "Готівкову оплату можна скасувати лише у відкритій зміні, де її провели. Закриту зміну не змінюємо заднім числом.")
                return redirect("settlements_home")
            reversal = _reverse_settlement_cash(payment, shift)
            if reversal is None:
                messages.error(request, "У касі недостатньо готівки для сторнування надходження. Спочатку звірте касу.")
                return redirect("settlements_home")
            drawer_after, counts, kind, signed_amount = reversal
            reversal_operation = CashOperation.objects.create(
                company=request.company, shift=shift, user=request.user, kind=kind,
                amount=signed_amount,
                detail={"source": "settlement_reversal", "settlement_payment_id": payment.pk,
                        "counts": cashlib.store(counts)},
                note=f"Сторнування оплати #{payment.pk}",
            )
            payment.reversal_operation = reversal_operation
            shift.drawer = cashlib.store(drawer_after)
            shift.save(update_fields=["drawer"])
        payment.is_void = True
        payment.voided_at = timezone.now()
        payment.voided_by = request.user
        payment.void_reason = form.cleaned_data["reason"].strip()
        payment.save(update_fields=["is_void", "voided_at", "voided_by", "void_reason", "reversal_operation"])
    messages.success(request, "Оплату скасовано; її касовий рух сторновано окремою операцією, історію збережено.")
    return redirect("settlements_home")


@tenant_staff_only
@company_required
def customer_account(request, pk):
    from .models import Customer

    customer = get_object_or_404(Customer.objects.filter(company=request.company), pk=pk)
    sales = customer.sales.filter(company=request.company, status=Sale.DONE).select_related(
        "cashier", "car"
    ).prefetch_related("items__product", "settlement_payments", "returns")
    rows = []
    totals = {"sales": D0, "checkout_paid": D0, "later_paid": D0, "returned": D0, "debt": D0}
    for sale in sales:
        later_paid = _paid_for(sale)
        returned = sum((item.total for item in sale._prefetched_objects_cache.get("returns", [])), D0)
        balance = _balance_for(sale)
        totals["sales"] += sale.total
        totals["checkout_paid"] += sale.cash_amount + sale.card_amount
        totals["later_paid"] += later_paid
        totals["returned"] += returned
        totals["debt"] += balance
        rows.append({"document": sale, "paid_later": later_paid, "returned": returned, "balance": balance})
    payments = SettlementPayment.objects.filter(company=request.company, sale__customer=customer).select_related(
        "sale", "created_by", "voided_by"
    )[:200]
    return render(request, "core/customer_detail.html", {
        "c": customer, "sales": sales, "sale_rows": rows, "account_totals": totals,
        "account_payments": payments,
    })


@tenant_staff_only
@company_required
def supplier_account(request, pk):
    from .models import Supplier

    supplier = get_object_or_404(Supplier.objects.filter(company=request.company), pk=pk)
    purchases = supplier.purchase_set.filter(company=request.company).select_related(
        "user"
    ).prefetch_related("items__product", "settlement_payments")
    rows = []
    totals = {"purchases": D0, "paid": D0, "debt": D0}
    for purchase in purchases:
        paid = _paid_for(purchase)
        balance = _balance_for(purchase)
        totals["purchases"] += purchase.total
        totals["paid"] += paid
        totals["debt"] += balance
        rows.append({"document": purchase, "paid": paid, "balance": balance})
    payments = SettlementPayment.objects.filter(company=request.company, purchase__supplier=supplier).select_related(
        "purchase", "created_by", "voided_by"
    )[:200]
    return render(request, "core/supplier_detail.html", {
        "supplier": supplier, "purchase_rows": rows, "account_totals": totals,
        "account_payments": payments,
    })
