from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .service_forms import ServiceAppointmentForm, ServiceOrderForm, ServiceOrderLineForm
from .service_models import ServiceAppointment, ServiceOrder, ServiceOrderLine


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
def service_appointment_new(request):
    form = ServiceAppointmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.created_by = request.user
        try:
            obj.full_clean()
            obj.save()
        except Exception as exc:
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
    return render(request, "core/service_order_detail.html", {"order": order, "line_form": line_form})


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
