from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum, F, DecimalField, ExpressionWrapper
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .service_forms import ServiceAppointmentForm, ServiceOrderForm, ServiceOrderLineForm
from .service_models import ServiceAppointment, ServiceOrder, ServiceOrderLine, ServicePayment
from .models import Product, Shift, CashOperation
from . import cash as cashlib
from .services import StockError, add_movement


def staff(view):
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


@staff
def service_home(request):
    appointments = ServiceAppointment.objects.select_related("customer", "car", "assigned_to").order_by("scheduled_at")[:80]
    orders = ServiceOrder.objects.select_related("customer", "car", "mechanic").order_by("-created_at")[:30]
    return render(request, "core/service_home.html", {"appointments": appointments, "orders": orders})


@staff
def service_reports(request):
    """Operational and financial overview for the current calendar month."""
    today = timezone.localdate()
    month_start = today.replace(day=1)
    month_orders = ServiceOrder.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today)
    month_payments = ServicePayment.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today)
    line_total = ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    month_lines = ServiceOrderLine.objects.filter(
        order__created_at__date__gte=month_start, order__created_at__date__lte=today
    )
    labor_total = month_lines.filter(kind=ServiceOrderLine.LABOR).aggregate(total=Sum(line_total))["total"] or Decimal("0")
    parts_total = month_lines.filter(kind=ServiceOrderLine.PART).aggregate(total=Sum(line_total))["total"] or Decimal("0")
    collected = month_payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    outstanding = sum(
        (order.balance_due for order in ServiceOrder.objects.exclude(status__in=["cancelled", "delivered"]).prefetch_related("lines")),
        Decimal("0"),
    )
    return render(request, "core/service_reports.html", {
        "month_start": month_start, "today": today, "orders_count": month_orders.count(),
        "labor_total": labor_total, "parts_total": parts_total, "service_total": labor_total + parts_total,
        "collected": collected, "outstanding": outstanding,
        "status_counts": [
            (label, ServiceOrder.objects.filter(status=code).count())
            for code, label in ServiceOrder.STATUS if code != "cancelled"
        ],
        "recent_payments": month_payments.select_related("order", "received_by").order_by("-created_at")[:30],
    })


@staff
def service_appointment_new(request):
    form = ServiceAppointmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.created_by = request.user
        try:
            obj.full_clean()
            # Appointments are exclusive for the same vehicle and, when
            # assigned, for the same mechanic. Check the full possible
            # 12-hour duration window, not only identical start times.
            window_end = obj.scheduled_at + timedelta(minutes=obj.duration_minutes)
            # Query all active appointments that begin before the proposed
            # end; duration is variable, so a fixed look-back window can miss
            # a long appointment that began earlier.
            candidates = ServiceAppointment.objects.select_for_update().filter(
                status__in=["booked", "arrived"],
                scheduled_at__lt=window_end,
            )
            conflict = None
            for existing in candidates:
                existing_end = existing.scheduled_at + timedelta(minutes=existing.duration_minutes)
                overlaps = obj.scheduled_at < existing_end and existing.scheduled_at < window_end
                same_vehicle = existing.car_id == obj.car_id
                same_mechanic = obj.assigned_to_id and existing.assigned_to_id == obj.assigned_to_id
                if overlaps and (same_vehicle or same_mechanic):
                    conflict = existing
                    break
            if conflict:
                raise ValidationError(
                    "Конфлікт запису: автомобіль або призначений майстер уже зайнятий у цей час "
                    f"(запис #{conflict.pk})."
                )
            obj.save()
        except (ValidationError, ValueError) as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Запис на СТО створено.")
            return redirect("service_home")
    return render(request, "core/service_appointment_form.html", {"form": form})


@staff
def service_order_new(request):
    form = ServiceOrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.created_by = request.user
        try:
            obj.full_clean()
            obj.save()
            if obj.appointment_id:
                ServiceAppointment.objects.filter(pk=obj.appointment_id).update(status="converted")
        except Exception as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Замовлення-наряд створено.")
            return redirect("service_order_detail", pk=obj.pk)
    return render(request, "core/service_order_form.html", {"form": form})


@staff
def service_order_detail(request, pk):
    order = get_object_or_404(ServiceOrder.objects.select_related("customer", "car", "mechanic"), pk=pk)
    line_form = ServiceOrderLineForm(request.POST or None)
    editable = order.status in {"draft", "diagnosis", "approval"}
    if request.method == "POST" and "add_line" in request.POST and not editable:
        messages.error(request, "Позиції можна змінювати лише до погодження наряду.")
        return redirect("service_order_detail", pk=order.pk)
    if request.method == "POST" and "add_line" in request.POST and line_form.is_valid():
        line = line_form.save(commit=False)
        line.order = order
        if line.kind == ServiceOrderLine.PART:
            if not line.product_id:
                line_form.add_error("product", "Для запчастини виберіть товар зі складу.")
            else:
                line.description = line.description or line.product.name
                line.unit_cost = line.product.cost_price
                if line.unit_price == 0:
                    line.unit_price = line.product.price
        else:
            line.product = None
            line.unit_cost = Decimal("0")
        if not line_form.errors:
            try:
                line.full_clean()
                line.save()
            except Exception as exc:
                line_form.add_error(None, str(exc))
            else:
                messages.success(request, "Позицію додано до наряду.")
                return redirect("service_order_detail", pk=order.pk)
    history = ServiceOrder.objects.filter(car=order.car).exclude(pk=order.pk).prefetch_related("lines").order_by("-created_at")[:20]
    transitions = {
        "draft": {"diagnosis", "cancelled"},
        "diagnosis": {"approval", "cancelled"},
        "approval": {"approved", "cancelled"},
        "approved": {"in_progress", "waiting_parts", "cancelled"},
        "in_progress": {"waiting_parts", "completed", "cancelled"},
        "waiting_parts": {"in_progress", "cancelled"},
        "completed": {"delivered"},
        "delivered": set(), "cancelled": set(),
    }
    next_statuses = transitions.get(order.status, set())
    status_options = [choice for choice in ServiceOrder.STATUS if choice[0] in next_statuses]
    return render(request, "core/service_order_detail.html", {
        "order": order, "line_form": line_form, "editable": editable, "vehicle_history": history,
        "status_options": status_options, "payments": order.payments.select_related("received_by", "shift").all(),
    })


@staff
@require_POST
@transaction.atomic
def service_order_payment(request, pk):
    order = get_object_or_404(ServiceOrder.objects.select_for_update(), pk=pk)
    if order.status == "cancelled":
        messages.error(request, "Не можна приймати оплату за скасованим нарядом.")
        return redirect("service_order_detail", pk=pk)
    try:
        amount = Decimal(request.POST.get("amount", "").replace(",", "."))
    except Exception:
        messages.error(request, "Вкажіть коректну суму оплати.")
        return redirect("service_order_detail", pk=pk)
    method = request.POST.get("method", "")
    if method not in {"cash", "card"}:
        messages.error(request, "Оберіть готівку або картку.")
        return redirect("service_order_detail", pk=pk)
    if amount <= 0 or amount.quantize(Decimal("0.01")) != amount:
        messages.error(request, "Сума має бути додатною та містити не більше двох знаків після коми.")
        return redirect("service_order_detail", pk=pk)
    balance = order.balance_due
    if amount > balance:
        messages.error(request, "Оплата перевищує залишок до сплати (" + str(balance) + " грн).")
        return redirect("service_order_detail", pk=pk)

    # Validate the payment before touching the cash drawer. Since validation
    # errors are reported as a redirect, catching one after cash mutations
    # would otherwise allow the surrounding atomic transaction to commit them.
    payment = ServicePayment(
        order=order, amount=amount, method=method,
        note=request.POST.get("note", "").strip()[:200],
        received_by=request.user,
    )
    try:
        payment.full_clean()
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("service_order_detail", pk=pk)

    shift = None
    if method == "cash":
        shift = Shift.objects.select_for_update().filter(cashier=request.user, closed_at__isnull=True).first()
        if shift is None:
            messages.error(request, "Для готівкової оплати відкрийте касову зміну.")
            return redirect("service_order_detail", pk=pk)
        amount_kop = cashlib.to_kop(amount)
        incoming = cashlib.decompose(amount_kop)
        drawer = cashlib.clean(shift.drawer)
        shift.drawer = cashlib.store(cashlib.add(drawer, incoming))
        shift.save(update_fields=["drawer"])
        CashOperation.objects.create(
            shift=shift, user=request.user, kind="in", amount=amount,
            note=("Оплата СТО: наряд #" + str(order.pk))[:200],
            detail={str(k): v for k, v in incoming.items()},
        )
    payment.shift = shift
    payment.save()

    order.paid_amount += amount
    order.payment_note = payment.note
    order.save(update_fields=["paid_amount", "payment_note", "updated_at"])
    messages.success(request, "Оплату " + str(amount) + " грн зафіксовано (" + payment.get_method_display().lower() + ").")
    return redirect("service_order_detail", pk=pk)


@staff
@require_POST
def service_order_issue_parts(request, pk):
    """Списати запчастини з наряду атомарно, не допускаючи повторного списання."""
    order = get_object_or_404(ServiceOrder, pk=pk)
    if order.status not in {"approved", "in_progress", "waiting_parts"}:
        messages.error(request, "Видавати запчастини можна лише після погодження наряду.")
        return redirect("service_order_detail", pk=pk)
    try:
        with transaction.atomic():
            lines = list(
                ServiceOrderLine.objects.select_for_update()
                .filter(order=order, kind=ServiceOrderLine.PART, issued=False)
                .select_related("product").order_by("product_id", "pk")
            )
            if not lines:
                raise StockError("Немає невиданих запчастин для списання.")
            totals = {}
            for line in lines:
                totals[line.product_id] = totals.get(line.product_id, Decimal("0")) + line.quantity
            locked = {
                p.pk: p for p in Product.objects.select_for_update().filter(pk__in=sorted(totals)).order_by("pk")
            }
            for product_id, qty in totals.items():
                product = locked[product_id]
                if product.current_stock < qty:
                    raise StockError(
                        f"Недостатньо залишку «{product.name}»: потрібно {qty}, є {product.current_stock}."
                    )
            for line in lines:
                add_movement(
                    locked[line.product_id], -line.quantity, "writeoff",
                    f"STO-{order.pk:06d}", request.user,
                    note=f"Запчастини до наряду #{order.pk}: {line.description}"[:200],
                )
                line.issued = True
                line.save(update_fields=["issued"])
        messages.success(request, f"Запчастини списано зі складу: {len(lines)} позицій.")
    except StockError as exc:
        messages.error(request, str(exc))
    return redirect("service_order_detail", pk=pk)


@staff
@require_POST
def service_order_status(request, pk):
    allowed = {s[0] for s in ServiceOrder.STATUS}
    order = get_object_or_404(ServiceOrder, pk=pk)
    target = request.POST.get("status", "")
    transitions = {
        "draft": {"diagnosis", "cancelled"},
        "diagnosis": {"approval", "cancelled"},
        "approval": {"approved", "cancelled"},
        "approved": {"in_progress", "waiting_parts", "cancelled"},
        "in_progress": {"waiting_parts", "completed", "cancelled"},
        "waiting_parts": {"in_progress", "cancelled"},
        "completed": {"delivered"},
        "delivered": set(), "cancelled": set(),
    }
    if target not in allowed or target not in transitions.get(order.status, set()):
        messages.error(request, "Недопустимий перехід статусу наряду.")
    elif target in {"completed", "delivered"} and order.lines.count() == 0:
        messages.error(request, "Не можна завершити порожній наряд.")
    elif target == "completed" and order.lines.filter(kind=ServiceOrderLine.PART, issued=False).exists():
        messages.error(request, "Перед завершенням ремонту спишіть зі складу всі запчастини наряду.")
    elif target == "delivered" and order.balance_due > 0:
        messages.error(request, "Не можна видати автомобіль клієнту: залишок до сплати " + str(order.balance_due) + " грн.")
    else:
        order.status = target
        if target == "approved":
            order.approved_at = timezone.now()
        if target == "completed":
            order.completed_at = timezone.now()
        if target == "delivered":
            order.delivered_at = timezone.now()
        order.save(update_fields=["status", "approved_at", "completed_at", "delivered_at", "updated_at"])
        messages.success(request, "Статус наряду оновлено.")
    return redirect("service_order_detail", pk=pk)
