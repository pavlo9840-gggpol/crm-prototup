import re
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Customer, Sale, Shipment
from .novaposhta import NovaPoshtaError, create_ttn, track_ttn


def _staff(request):
    if not request.user.is_staff:
        raise PermissionDenied


@login_required
def shipment_list(request):
    _staff(request)
    q = (request.GET.get("q") or "").strip()[:120]
    status = (request.GET.get("status") or "").strip()
    rows = Shipment.objects.select_related("customer", "sale", "created_by")
    if q:
        rows = rows.filter(
            Q(ttn__icontains=q) | Q(recipient_name__icontains=q) |
            Q(recipient_phone__icontains=q) | Q(recipient_city__icontains=q)
        )
    if status in dict(Shipment.STATUS):
        rows = rows.filter(status=status)
    return render(request, "core/shipment_list.html", {
        "shipments": rows[:300], "q": q, "status": status,
        "statuses": Shipment.STATUS, "total_count": rows.count(),
    })


@login_required
def shipment_new(request):
    _staff(request)
    customers = Customer.objects.all()
    sales = Sale.objects.order_by("-created_at")[:300]
    if request.method == "POST":
        name = (request.POST.get("recipient_name") or "").strip()
        phone = (request.POST.get("recipient_phone") or "").strip()
        if not name or not phone:
            messages.error(request, "Вкажіть ім'я та телефон одержувача.")
            return render(request, "core/shipment_form.html", {"customers": customers, "sales": sales, "data": request.POST})
        try:
            weight = Decimal((request.POST.get("weight") or "1").replace(",", "."))
            declared = Decimal((request.POST.get("declared_cost") or "0").replace(",", "."))
            cod = Decimal((request.POST.get("cod_amount") or "0").replace(",", "."))
            seats = int(request.POST.get("seats_amount") or "1")
            if weight <= 0 or declared < 0 or cod < 0 or seats < 1 or seats > 100:
                raise ValueError
        except (InvalidOperation, ValueError):
            messages.error(request, "Перевірте вагу, оголошену вартість, післяплату та кількість місць.")
            return render(request, "core/shipment_form.html", {"customers": customers, "sales": sales, "data": request.POST})
        customer = Customer.objects.filter(pk=request.POST.get("customer")).first() if request.POST.get("customer") else None
        sale = Sale.objects.filter(pk=request.POST.get("sale")).first() if request.POST.get("sale") else None
        shipment = Shipment.objects.create(
            customer=customer, sale=sale, recipient_name=name, recipient_phone=phone,
            recipient_city=(request.POST.get("recipient_city") or "").strip()[:150],
            recipient_city_ref=(request.POST.get("recipient_city_ref") or "").strip()[:80],
            recipient_warehouse=(request.POST.get("recipient_warehouse") or "").strip()[:250],
            recipient_warehouse_ref=(request.POST.get("recipient_warehouse_ref") or "").strip()[:80],
            description=(request.POST.get("description") or "Автозапчастини").strip()[:200],
            weight=weight, declared_cost=declared, cod_amount=cod, seats_amount=seats,
            created_by=request.user,
        )
        messages.success(request, "Чернетку відправлення збережено. Перевірте дані й створіть ТТН.")
        return redirect("shipment_list")
    return render(request, "core/shipment_form.html", {"customers": customers, "sales": sales, "data": {}})


@login_required
@require_POST
def shipment_create_ttn(request, pk):
    _staff(request)
    shipment = get_object_or_404(Shipment, pk=pk)
    if shipment.ttn:
        messages.info(request, f"Для цього відправлення вже є ТТН {shipment.ttn}.")
        return redirect("shipment_list")
    try:
        shipment.ttn = create_ttn(shipment)
        shipment.status = "created"
        shipment.status_text = "Експрес-накладну створено"
        shipment.api_error = ""
        shipment.save(update_fields=["ttn", "status", "status_text", "api_error", "updated_at"])
        messages.success(request, f"Створено ТТН {shipment.ttn}.")
    except NovaPoshtaError as exc:
        shipment.api_error = str(exc)[:500]
        shipment.save(update_fields=["api_error", "updated_at"])
        messages.error(request, f"Не вдалося створити ТТН: {exc}")
    return redirect("shipment_list")


@login_required
@require_POST
def shipment_track(request, pk):
    _staff(request)
    shipment = get_object_or_404(Shipment, pk=pk)
    if not shipment.ttn:
        messages.error(request, "Спочатку створіть ТТН або внесіть номер накладної.")
        return redirect("shipment_list")
    try:
        result = track_ttn(shipment.ttn, shipment.recipient_phone)
        text = str(result.get("Status") or result.get("StatusCode") or "Статус отримано")
        code = str(result.get("StatusCode") or "")
        if code in {"9", "10", "11"} or "отримано" in text.lower() or "вручено" in text.lower():
            shipment.status = "delivered"
        elif "відділен" in text.lower() or "прибул" in text.lower():
            shipment.status = "arrived"
        elif "поверн" in text.lower():
            shipment.status = "returned"
        else:
            shipment.status = "in_transit"
        shipment.status_text = text[:250]
        shipment.last_tracked_at = timezone.now()
        shipment.api_error = ""
        shipment.save(update_fields=["status", "status_text", "last_tracked_at", "api_error", "updated_at"])
        messages.success(request, f"Статус ТТН {shipment.ttn}: {shipment.status_text}")
    except NovaPoshtaError as exc:
        shipment.api_error = str(exc)[:500]
        shipment.save(update_fields=["api_error", "updated_at"])
        messages.error(request, f"Не вдалося оновити статус: {exc}")
    return redirect("shipment_list")
