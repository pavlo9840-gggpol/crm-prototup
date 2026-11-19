"""Customer and supplier document settlement register."""
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import cash as cashlib
from .models import AccountAdvance, BankAccount, BankReconciliation, BankTransaction, CashOperation, Customer, Purchase, Sale, SettlementPayment, Shift, Supplier, SupplierCompensation, SupplierReturn
from .tenant_context import company_required
from .views import tenant_staff_only

D0 = Decimal("0.00")


class SettlementPaymentForm(forms.Form):
    advance = forms.ModelChoiceField(queryset=AccountAdvance.objects.none(), required=False, label="Використати аванс")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), label="Сума оплати")
    payment_method = forms.ChoiceField(choices=SettlementPayment.PAYMENT_METHODS, label="Спосіб оплати")
    document_ref = forms.CharField(max_length=80, required=False, label="Документ оплати")
    note = forms.CharField(max_length=250, required=False, label="Примітка")

    def __init__(self, *args, company=None, party_type=None, party_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = AccountAdvance.objects.filter(company=company, party_type=party_type, is_void=False, remaining__gt=0)
        if party_type == AccountAdvance.CUSTOMER:
            qs = qs.filter(customer_id=party_id)
        else:
            qs = qs.filter(supplier_id=party_id)
        self.fields["advance"].queryset = qs


class AdvanceForm(forms.Form):
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), label="Сума авансу")
    payment_method = forms.ChoiceField(choices=[("cash", "Готівка"), ("card", "Картка"), ("bank", "Банківський рахунок")])
    document_ref = forms.CharField(max_length=80, required=False, label="Документ оплати")
    note = forms.CharField(max_length=250, required=False, label="Примітка")


class SupplierCompensationForm(forms.Form):
    kind = forms.ChoiceField(choices=SupplierCompensation.KINDS, label="Фактичний спосіб компенсації")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), label="Сума компенсації")
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.none(), required=False, label="Банківський рахунок")
    note = forms.CharField(max_length=250, required=False, label="Примітка")

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = BankAccount.objects.filter(company=company)


class SupplierCompensationVoidForm(forms.Form):
    reason = forms.CharField(max_length=250, label="Причина скасування")


class SettlementVoidForm(forms.Form):
    reason = forms.CharField(max_length=250, label="Причина скасування")


def _paid_for(document):
    cached = getattr(document, "_prefetched_objects_cache", {}).get("settlement_payments")
    if cached is not None:
        return sum((payment.amount for payment in cached if not payment.is_void), D0)
    return document.settlement_payments.filter(company=document.company, is_void=False).aggregate(total=Sum("amount"))["total"] or D0


def _supplier_compensated_for(document):
    cached = getattr(document, "_prefetched_objects_cache", {}).get("supplier_compensations")
    if cached is not None:
        return sum((entry.amount for entry in cached if not entry.is_void), D0)
    return document.supplier_compensations.filter(company=document.company, is_void=False).aggregate(
        total=Sum("amount")
    )["total"] or D0


def _supplier_net_balance_for(document):
    return document.total - _paid_for(document) - _supplier_compensated_for(document)


def _balance_for(document):
    if isinstance(document, Purchase):
        return max(D0, _supplier_net_balance_for(document))
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
    purchase_docs = Purchase.objects.filter(company=request.company).select_related("supplier").prefetch_related(
        "settlement_payments", "supplier_compensations"
    )
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
        "cash_operation__shift", "reversal_operation", "advance",
    )[:200]
    return render(request, "core/settlements.html", {
        "supplier_rows": supplier_rows, "customer_rows": customer_rows,
        "supplier_total": supplier_total, "customer_total": customer_total,
        "payments": payments,
        "advances": AccountAdvance.objects.filter(company=request.company).select_related("customer", "supplier", "created_by").order_by("-created_at")[:200],
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
        party_type = AccountAdvance.CUSTOMER if document_type == "sale" else AccountAdvance.SUPPLIER
        party_id = document.customer_id if document_type == "sale" else document.supplier_id
        form = SettlementPaymentForm(request.POST, company=request.company, party_type=party_type, party_id=party_id)
        if not form.is_valid():
            messages.error(request, "Перевірте суму та спосіб оплати.")
            return redirect("purchase_detail" if document_type == "purchase" else "sale_detail", pk=document.pk)
        balance = _balance_for(document)
        amount = form.cleaned_data["amount"]
        payment_method = form.cleaned_data["payment_method"]
        advance = form.cleaned_data.get("advance")
        if payment_method == "advance" and advance:
            advance = AccountAdvance.objects.select_for_update().filter(
                pk=advance.pk, company=request.company, is_void=False
            ).first()
        shift = None
        cash_plan = None
        if balance <= 0:
            messages.error(request, "За цим документом немає непогашеної заборгованості.")
        elif amount > balance:
            messages.error(request, f"Сума перевищує залишок боргу: {balance:.2f} ₴.")
        elif payment_method == "advance" and (not advance or advance.remaining < amount):
            messages.error(request, "Оберіть аванс із достатнім невикористаним залишком.")
        elif payment_method == "advance" and (advance.party_type != party_type or (advance.customer_id if party_type == AccountAdvance.CUSTOMER else advance.supplier_id) != party_id):
            messages.error(request, "Цей аванс належить іншому контрагенту.")
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
                    note=form.cleaned_data["note"].strip(), advance=advance if payment_method == "advance" else None, **kwargs,
                )
                if payment_method == "advance":
                    advance.remaining -= amount
                    advance.save(update_fields=["remaining"])
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
        if BankReconciliation.objects.filter(settlement_payment=payment, is_void=False).exists():
            messages.error(request, "Спочатку скасуйте банківську звірку цієї оплати.")
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
        if payment.advance_id:
            advance = AccountAdvance.objects.select_for_update().get(pk=payment.advance_id, company=request.company)
            if advance.is_void:
                messages.error(request, "Аванс уже скасовано; неможливо відновити його залишок.")
                return redirect("settlements_home")
            advance.remaining += payment.amount
            advance.save(update_fields=["remaining"])
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
        "sale", "created_by", "voided_by", "advance"
    )[:200]
    advances = AccountAdvance.objects.filter(company=request.company, customer=customer).order_by("-created_at")
    totals["advance_balance"] = advances.filter(is_void=False).aggregate(total=Sum("remaining"))["total"] or D0
    return render(request, "core/customer_detail.html", {
        "c": customer, "sales": sales, "sale_rows": rows, "account_totals": totals,
        "account_payments": payments, "account_advances": advances,
    })


@tenant_staff_only
@company_required
def supplier_account(request, pk):
    from .models import Supplier

    supplier = get_object_or_404(Supplier.objects.filter(company=request.company), pk=pk)
    purchases = supplier.purchase_set.filter(company=request.company).select_related(
        "user"
    ).prefetch_related("items__product", "settlement_payments", "supplier_compensations")
    rows = []
    totals = {"purchases": D0, "paid": D0, "compensated": D0, "debt": D0, "supplier_credit": D0}
    for purchase in purchases:
        paid = _paid_for(purchase)
        compensated = _supplier_compensated_for(purchase)
        net_balance = purchase.total - paid - compensated
        balance = max(D0, net_balance)
        supplier_credit = max(D0, -net_balance)
        totals["purchases"] += purchase.total
        totals["paid"] += paid
        totals["compensated"] += compensated
        totals["debt"] += balance
        totals["supplier_credit"] += supplier_credit
        rows.append({"document": purchase, "paid": paid, "compensated": compensated,
                     "balance": balance, "supplier_credit": supplier_credit})
    payments = SettlementPayment.objects.filter(company=request.company, purchase__supplier=supplier).select_related(
        "purchase", "created_by", "voided_by", "advance"
    )[:200]
    compensations = SupplierCompensation.objects.filter(
        company=request.company, supplier=supplier
    ).select_related("purchase", "supplier_return", "created_by", "bank_account")[:200]
    advances = AccountAdvance.objects.filter(company=request.company, supplier=supplier).order_by("-created_at")
    totals["advance_balance"] = advances.filter(is_void=False).aggregate(total=Sum("remaining"))["total"] or D0
    return render(request, "core/supplier_detail.html", {
        "supplier": supplier, "purchase_rows": rows, "account_totals": totals,
        "account_payments": payments, "account_advances": advances, "account_compensations": compensations,
    })


@tenant_staff_only
@company_required
def supplier_return_compensate(request, pk):
    if request.method != "POST":
        return redirect("settlements_home")
    form = SupplierCompensationForm(request.POST, company=request.company)
    if not form.is_valid():
        messages.error(request, "Перевірте спосіб, суму та банківський рахунок компенсації.")
        return redirect(request.META.get("HTTP_REFERER") or "settlements_home")

    with transaction.atomic():
        returned = get_object_or_404(
            SupplierReturn.objects.select_for_update().select_related(
                "supplier", "purchase_item__purchase"
            ).filter(company=request.company),
            pk=pk,
        )
        purchase = Purchase.objects.select_for_update().get(
            pk=returned.purchase_item.purchase_id, company=request.company
        )
        already_compensated = SupplierCompensation.objects.filter(
            company=request.company, supplier_return=returned
        ).aggregate(total=Sum("amount"))["total"] or D0
        remaining = returned.total - already_compensated
        amount = form.cleaned_data["amount"]
        kind = form.cleaned_data["kind"]
        if amount > remaining:
            messages.error(request, f"Сума перевищує невідшкодований залишок повернення: {remaining:.2f} ₴.")
            return redirect("purchase_detail", pk=purchase.pk)

        cash_operation = None
        bank_transaction = None
        bank_account = None
        if kind == SupplierCompensation.CASH:
            if cashlib.to_kop(amount) % 10:
                messages.error(request, "Готівкове повернення має бути кратним 0,10 ₴ через доступні номінали каси.")
                return redirect("purchase_detail", pk=purchase.pk)
            shift = Shift.objects.select_for_update().filter(
                company=request.company, cashier=request.user, closed_at__isnull=True
            ).first()
            if not shift:
                messages.error(request, "Для повернення готівки відкрийте касову зміну під своїм обліковим записом.")
                return redirect("purchase_detail", pk=purchase.pk)
            counts = cashlib.decompose(cashlib.to_kop(amount))
            drawer_after = cashlib.add(cashlib.clean(shift.drawer), counts)
            cash_operation = CashOperation.objects.create(
                company=request.company, shift=shift, user=request.user, kind="in", amount=amount,
                detail={"source": "supplier_return_compensation", "supplier_return_id": returned.pk,
                        "counts": cashlib.store(counts)},
                note=f"Повернення від постачальника · {returned.number}"[:200],
            )
            shift.drawer = cashlib.store(drawer_after)
            shift.save(update_fields=["drawer"])
        elif kind == SupplierCompensation.BANK:
            bank_account = form.cleaned_data.get("bank_account")
            if not bank_account or bank_account.company_id != request.company.pk:
                messages.error(request, "Оберіть банківський рахунок цього магазину.")
                return redirect("purchase_detail", pk=purchase.pk)
            bank_transaction = BankTransaction.objects.create(
                company=request.company, account=bank_account, transaction_date=timezone.localdate(),
                direction=BankTransaction.INCOME, category="Повернення від постачальника",
                amount=amount, counterparty=returned.supplier.name,
                document_ref=returned.number, note=form.cleaned_data["note"].strip()[:250],
                created_by=request.user,
            )

        compensation = SupplierCompensation.objects.create(
            company=request.company, supplier_return=returned, purchase=purchase,
            supplier=returned.supplier, kind=kind, amount=amount,
            bank_account=bank_account, cash_operation=cash_operation,
            bank_transaction=bank_transaction, note=form.cleaned_data["note"].strip(),
            created_by=request.user,
        )
    messages.success(
        request,
        f"Компенсацію {compensation.number} на {amount:.2f} ₴ проведено. "
        "Вона врахована у взаєморозрахунках із постачальником.",
    )
    return redirect("purchase_detail", pk=purchase.pk)


@tenant_staff_only
@company_required
def supplier_compensation_void(request, pk):
    if request.method != "POST":
        return redirect("settlements_home")
    form = SupplierCompensationVoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Вкажіть причину скасування компенсації.")
        return redirect("settlements_home")

    with transaction.atomic():
        compensation = get_object_or_404(
            SupplierCompensation.objects.select_for_update().select_related(
                "purchase", "supplier", "cash_operation__shift", "bank_account", "bank_transaction"
            ).filter(company=request.company, is_void=False),
            pk=pk,
        )
        reversal_cash = None
        reversal_bank = None
        if compensation.kind == SupplierCompensation.CASH:
            original_shift_id = compensation.cash_operation.shift_id
            shift = Shift.objects.select_for_update().filter(
                pk=original_shift_id, company=request.company, closed_at__isnull=True
            ).first()
            if not shift:
                messages.error(request, "Готівкову компенсацію можна скасувати лише у відкритій касовій зміні, де її провели.")
                return redirect("purchase_detail", pk=compensation.purchase_id)
            counts = cashlib.make_change(cashlib.to_kop(compensation.amount), cashlib.clean(shift.drawer))
            if counts is None:
                messages.error(request, "У касі недостатньо готівки для сторнування компенсації.")
                return redirect("purchase_detail", pk=compensation.purchase_id)
            drawer_after = cashlib.add(cashlib.clean(shift.drawer), counts, -1)
            reversal_cash = CashOperation.objects.create(
                company=request.company, shift=shift, user=request.user, kind="out",
                amount=-compensation.amount,
                detail={"source": "supplier_compensation_reversal", "supplier_compensation_id": compensation.pk,
                        "counts": cashlib.store(counts)},
                note=f"Сторнування компенсації {compensation.number}"[:200],
            )
            shift.drawer = cashlib.store(drawer_after)
            shift.save(update_fields=["drawer"])
        elif compensation.kind == SupplierCompensation.BANK:
            if not compensation.bank_transaction_id or compensation.bank_transaction.is_void:
                messages.error(request, "Початкову банківську операцію вже скасовано або не знайдено.")
                return redirect("purchase_detail", pk=compensation.purchase_id)
            reversal_bank = BankTransaction.objects.create(
                company=request.company, account=compensation.bank_account,
                transaction_date=timezone.localdate(), direction=BankTransaction.EXPENSE,
                category="Сторнування компенсації постачальника",
                amount=compensation.amount, counterparty=compensation.supplier.name,
                document_ref=compensation.number,
                note=form.cleaned_data["reason"].strip()[:250], created_by=request.user,
            )

        compensation.is_void = True
        compensation.voided_at = timezone.now()
        compensation.voided_by = request.user
        compensation.void_reason = form.cleaned_data["reason"].strip()
        compensation.reversal_cash_operation = reversal_cash
        compensation.reversal_bank_transaction = reversal_bank
        compensation.save(update_fields=[
            "is_void", "voided_at", "voided_by", "void_reason",
            "reversal_cash_operation", "reversal_bank_transaction",
        ])
    messages.success(request, f"Компенсацію {compensation.number} скасовано окремою сторнувальною операцією.")
    return redirect("purchase_detail", pk=compensation.purchase_id)


@tenant_staff_only
@company_required
@transaction.atomic
def advance_new(request, party_type, pk):
    if party_type == AccountAdvance.CUSTOMER:
        party = get_object_or_404(Customer.objects.filter(company=request.company), pk=pk)
        party_kwargs = {"customer": party, "party_type": AccountAdvance.CUSTOMER}
        is_customer = True
    elif party_type == AccountAdvance.SUPPLIER:
        party = get_object_or_404(Supplier.objects.filter(company=request.company), pk=pk)
        party_kwargs = {"supplier": party, "party_type": AccountAdvance.SUPPLIER}
        is_customer = False
    else:
        messages.error(request, "Невідомий тип контрагента.")
        return redirect("settlements_home")

    form = AdvanceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        amount = form.cleaned_data["amount"]
        method = form.cleaned_data["payment_method"]
        shift = None
        drawer_after = counts = kind = None
        if method == "cash":
            shift = Shift.objects.select_for_update().filter(
                company=request.company, cashier=request.user, closed_at__isnull=True
            ).first()
            if not shift:
                messages.error(request, "Для готівкового авансу відкрийте власну касову зміну.")
                return redirect("advance_new", party_type=party_type, pk=pk)
            drawer = cashlib.clean(shift.drawer)
            if is_customer:
                counts = cashlib.decompose(cashlib.to_kop(amount))
                drawer_after, kind = cashlib.add(drawer, counts), "in"
            else:
                counts = cashlib.make_change(cashlib.to_kop(amount), drawer)
                if counts is None:
                    messages.error(request, "У касі недостатньо готівки потрібного номіналу для авансу постачальнику.")
                    return redirect("advance_new", party_type=party_type, pk=pk)
                drawer_after, kind = cashlib.add(drawer, counts, -1), "out"

        with transaction.atomic():
            advance = AccountAdvance.objects.create(
                company=request.company, created_by=request.user, amount=amount, remaining=amount,
                payment_method=method, document_ref=form.cleaned_data["document_ref"].strip(),
                note=form.cleaned_data["note"].strip(), **party_kwargs,
            )
            if method == "cash":
                signed = amount if kind == "in" else -amount
                op = CashOperation.objects.create(
                    company=request.company, shift=shift, user=request.user, kind=kind, amount=signed,
                    detail={"source": "advance", "advance_id": advance.pk, "counts": cashlib.store(counts)},
                    note=("Отримано аванс від клієнта" if is_customer else "Видано аванс постачальнику") + f" · {party}",
                )
                advance.cash_operation = op
                advance.save(update_fields=["cash_operation"])
                shift.drawer = cashlib.store(drawer_after)
                shift.save(update_fields=["drawer"])
        messages.success(request, f"Аванс {amount:.2f} ₴ зареєстровано.")
        return redirect("customer_account" if is_customer else "supplier_account", pk=pk)

    return render(request, "core/advance_form.html", {
        "form": form, "party": party, "party_type": party_type,
        "title": "Аванс від клієнта" if is_customer else "Аванс постачальнику",
        "back_url": ("customer_account" if is_customer else "supplier_account"),
        "party_id": pk,
    })


@tenant_staff_only
@company_required
def advance_void(request, pk):
    if request.method != "POST":
        return redirect("settlements_home")
    form = SettlementVoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Вкажіть причину скасування.")
        return redirect("settlements_home")
    with transaction.atomic():
        advance = AccountAdvance.objects.select_for_update().filter(
            pk=pk, company=request.company, is_void=False
        ).select_related("cash_operation").first()
        if not advance:
            messages.error(request, "Аванс не знайдено або вже скасовано.")
            return redirect("settlements_home")
        if advance.applications.filter(is_void=False).exists():
            messages.error(request, "Не можна скасувати аванс, доки його частина використана в документах. Спочатку сторнуйте застосування.")
            return redirect("settlements_home")
        if advance.cash_operation_id:
            shift = Shift.objects.select_for_update().filter(
                pk=advance.cash_operation.shift_id, company=request.company, closed_at__isnull=True
            ).first()
            if not shift:
                messages.error(request, "Готівковий аванс можна скасувати лише у відкритій зміні, де його провели.")
                return redirect("settlements_home")
            original = advance.cash_operation
            drawer = cashlib.clean(shift.drawer)
            amount_kop = cashlib.to_kop(advance.amount)
            if original.kind == "in":
                counts = cashlib.make_change(amount_kop, drawer)
                if counts is None:
                    messages.error(request, "У касі недостатньо готівки для повернення авансу.")
                    return redirect("settlements_home")
                drawer_after, kind, signed = cashlib.add(drawer, counts, -1), "out", -advance.amount
            else:
                counts = cashlib.clean(original.detail.get("counts", {}))
                drawer_after, kind, signed = cashlib.add(drawer, counts), "in", advance.amount
            CashOperation.objects.create(
                company=request.company, shift=shift, user=request.user, kind=kind, amount=signed,
                detail={"source": "advance_reversal", "advance_id": advance.pk, "counts": cashlib.store(counts)},
                note=f"Сторнування авансу #{advance.pk}",
            )
            shift.drawer = cashlib.store(drawer_after)
            shift.save(update_fields=["drawer"])
        advance.is_void = True
        advance.remaining = Decimal("0.00")
        advance.voided_at = timezone.now()
        advance.voided_by = request.user
        advance.void_reason = form.cleaned_data["reason"].strip()
        advance.save(update_fields=["is_void", "remaining", "voided_at", "voided_by", "void_reason"])
    messages.success(request, "Аванс скасовано; історію та касове сторнування збережено.")
    return redirect("settlements_home")
