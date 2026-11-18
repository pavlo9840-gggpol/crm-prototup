"""Customer and supplier document settlement register."""
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Purchase, Sale, SettlementPayment
from .tenant_context import company_required
from .views import tenant_staff_only

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
        "sale__customer", "purchase__supplier", "created_by", "voided_by"
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
        if balance <= 0:
            messages.error(request, "За цим документом немає непогашеної заборгованості.")
        elif amount > balance:
            messages.error(request, f"Сума перевищує залишок боргу: {balance:.2f} ₴.")
        else:
            kwargs = {"purchase": document} if document_type == "purchase" else {"sale": document}
            SettlementPayment.objects.create(
                company=request.company, created_by=request.user, amount=amount,
                payment_method=form.cleaned_data["payment_method"],
                document_ref=form.cleaned_data["document_ref"].strip(),
                note=form.cleaned_data["note"].strip(), **kwargs,
            )
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
        ).select_related("sale", "purchase").first()
        if not payment:
            messages.error(request, "Оплату не знайдено або її вже скасовано.")
            return redirect("settlements_home")
        payment.is_void = True
        payment.voided_at = timezone.now()
        payment.voided_by = request.user
        payment.void_reason = form.cleaned_data["reason"].strip()
        payment.save(update_fields=["is_void", "voided_at", "voided_by", "void_reason"])
    messages.success(request, "Оплату скасовано; запис залишився в історії.")
    return redirect("settlements_home")
