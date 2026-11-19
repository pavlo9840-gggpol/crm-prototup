import json
import csv
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import (Count, DecimalField, ExpressionWrapper, F, Max, Prefetch, Q, Sum)
from django.db.models.functions import TruncDate
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import cash as cashlib, pricing, services
from .forms import AdjustForm, BusinessProfileForm, CategoryForm, CompanyForm, CustomerForm, ProductForm, SupplierForm
from .models import (BankAccount, CashOperation, CompanySettings, Category, Customer, CustomerCar, MarkupTier, PriceHistory, Product,
                     Purchase, PurchaseItem, PurchaseOrder, PurchaseOrderItem, Sale, SaleItem, SaleReturn, Shift, StockMovement, Supplier, SupplierItem, PriceRule, PayPlan, ProductBarcode, StockBatch, StockSerialUnit, InventoryCount, InventoryCountItem, SettlementPayment)
from .models import norm_plate, norm_sku
from .services import StockError
from .tenant_context import company_queryset, company_required

D0 = Decimal("0")
DEC = DecimalField(max_digits=14, decimal_places=2)


def staff_only(view):
    @login_required
    @wraps(view)
    def wrapper(request, *a, **k):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *a, **k)
    return wrapper


def tenant_staff_only(view):
    """Allow company owners/admins/managers without requiring global Django is_staff."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        membership = getattr(request, "staff_membership", None)
        if not request.user.is_superuser and (
            membership is None or membership.role not in ("owner", "admin", "manager")
        ):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def tenant_cashier_only(view):
    """Allow company managers and cashiers into POS/cash workflows, but not workshop-only members."""
    @login_required
    @company_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        membership = getattr(request, "staff_membership", None)
        if membership is None or membership.role not in ("owner", "admin", "manager", "cashier"):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def _jb(request):
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def dec(v, default=None):
    try:
        return Decimal(str(v).replace(",", "."))
    except (InvalidOperation, ValueError):
        return default


def get_date(request, key, default):
    try:
        return date.fromisoformat(request.GET.get(key, ""))
    except ValueError:
        return default


def _can_manage_company(request):
    membership = getattr(request, "staff_membership", None)
    return bool(membership and membership.role in ("owner", "admin", "manager"))


def done_sales(company=None):
    qs = Sale.objects.filter(status=Sale.DONE)
    return qs.filter(company=company) if company is not None else qs.none()


def current_shift(user, company=None):
    qs = Shift.objects.filter(cashier=user, closed_at__isnull=True)
    if company is not None:
        qs = qs.filter(company=company)
    return qs.first()


# ---------- Dashboard ----------
@login_required
@company_required
def dashboard(request):
    if not _can_manage_company(request):
        return redirect("pos")
    from .models import AccountAdvance
    from .settlement_views import _balance_for

    today = timezone.localdate()
    today_sales = done_sales(request.company).filter(created_at__date=today)
    month_sales = done_sales(request.company).filter(created_at__year=today.year, created_at__month=today.month)
    week = {r["day"]: r["s"] for r in done_sales(request.company).filter(created_at__date__gte=today - timedelta(days=6))
            .annotate(day=TruncDate("created_at")).values("day").annotate(s=Sum("total"))}
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    mx = max([week.get(d, D0) for d in days] + [Decimal("1")])
    bars = [{"day": d, "sum": week.get(d, D0), "h": int(week.get(d, D0) / mx * 100)} for d in days]

    # Reuse the same balance rules as the customer/supplier settlement ledger.
    # This prevents dashboard debt figures drifting from the accounting screens.
    purchase_docs = Purchase.objects.filter(company=request.company).select_related("supplier").prefetch_related("settlement_payments")
    sale_docs = Sale.objects.filter(company=request.company, status=Sale.DONE, customer__isnull=False).select_related("customer").prefetch_related("settlement_payments", "returns")
    supplier_debt = sum((_balance_for(doc) for doc in purchase_docs), D0)
    customer_debt = sum((_balance_for(doc) for doc in sale_docs), D0)
    customer_advances = AccountAdvance.objects.filter(
        company=request.company, party_type=AccountAdvance.CUSTOMER, is_void=False
    ).aggregate(total=Sum("remaining"))["total"] or D0
    supplier_advances = AccountAdvance.objects.filter(
        company=request.company, party_type=AccountAdvance.SUPPLIER, is_void=False
    ).aggregate(total=Sum("remaining"))["total"] or D0

    low_products = company_queryset(
        Product.objects.filter(is_active=True, current_stock__lte=F("min_stock")).select_related("category")
        .order_by("current_stock", "name"), request
    )[:8]
    ctx = {
        "today_total": today_sales.aggregate(s=Sum("total"))["s"] or D0,
        "today_count": today_sales.count(),
        "month_total": month_sales.aggregate(s=Sum("total"))["s"] or D0,
        "low_count": company_queryset(Product.objects.filter(is_active=True, current_stock__lte=F("min_stock")), request).count(),
        "low_products": low_products,
        "customer_debt": customer_debt, "supplier_debt": supplier_debt,
        "customer_advances": customer_advances, "supplier_advances": supplier_advances,
        "last_sales": done_sales(request.company).select_related("cashier")[:10],
        "last_moves": company_queryset(StockMovement.objects.select_related("product"), request)[:10],
        "bars": bars,
        "waiting_orders": company_queryset(SaleItem.objects.filter(company=request.company, sale__company=request.company, source=SaleItem.SUPPLIER, sale__status=Sale.DONE), request).filter(
            fulfil_status__in=[SaleItem.ORDERED, SaleItem.ARRIVED]).count(),
        "overdue_orders": company_queryset(SaleItem.objects.filter(company=request.company, sale__company=request.company, source=SaleItem.SUPPLIER, sale__status=Sale.DONE,
            fulfil_status=SaleItem.ARRIVED, pickup_deadline__lt=today), request).count(),
    }
    return render(request, "core/dashboard.html", ctx)

# ---------- Products / categories / suppliers ----------
@tenant_staff_only
@company_required
def product_list(request):
    supplier_items = company_queryset(SupplierItem.objects.select_related("supplier"), request)
    qs = company_queryset(
        Product.objects.select_related("category").prefetch_related(
            Prefetch("supplier_items", queryset=supplier_items)
        ),
        request,
    )
    q, cat = request.GET.get("q", "").strip(), request.GET.get("category", "")
    condition = request.GET.get("condition", "").strip()
    if condition in dict(Product.CONDITION_CHOICES):
        qs = qs.filter(condition=condition)
    if q:
        qn = norm_sku(q)
        cond = Q(name_lc__contains=q.lower()) | Q(sku__icontains=q) | Q(barcode__icontains=q) | Q(brand__icontains=q) | Q(extra_barcodes__barcode__icontains=q)
        if qn:
            cond |= Q(sku_norm__contains=qn)
        qs = qs.filter(cond).distinct()
    if cat:
        ids = [int(cat)] if cat.isdigit() else []
        ids += list(company_queryset(Category.objects.filter(parent_id__in=ids), request).values_list("id", flat=True))
        qs = qs.filter(category_id__in=ids)
    sort = request.GET.get("sort", "name")
    if sort.lstrip("-") in ("name", "current_stock", "price", "cost_price", "sku", "brand"):
        qs = qs.order_by(sort)
    from .designer_views import print_bar
    return render(request, "core/product_list.html",
                  {"products": qs, "categories": company_queryset(Category.objects.all(), request), "q": q, "cat": cat, "condition": condition, **print_bar("pricetag", request.company)})


def all_brands(request):
    """Бренди лише з активного магазину та його прайсів постачальників."""
    names = set(company_queryset(Product.objects.exclude(brand=""), request).values_list("brand", flat=True).distinct())
    names |= set(company_queryset(SupplierItem.objects.exclude(brand=""), request).values_list("brand", flat=True).distinct()[:3000])
    return sorted(names)[:3000]


PREFILL = ("brand", "sku", "name", "category", "unit", "cost_price", "price")


@tenant_staff_only
@company_required
def product_edit(request, pk=None):
    obj = get_object_or_404(company_queryset(Product.objects.all(), request), pk=pk) if pk else None
    old_prices = {field: getattr(obj, field) for field in ("price", "cost_price")} if obj else None
    initial = {k: request.GET[k] for k in PREFILL if request.GET.get(k)} if (not obj and request.method == "GET") else {}
    form = ProductForm(request.POST or None, request.FILES or None, instance=obj, initial=initial, company=request.company)
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        p.company = request.company
        with transaction.atomic():
            p.save()
            form.save_m2m()
            form.sync_additional_barcodes(p)
        if old_prices is not None:
            for field, old_value in old_prices.items():
                if old_value != getattr(p, field):
                    PriceHistory.objects.create(company=request.company, product=p, field=field, old_value=old_value,
                                                new_value=getattr(p, field), changed_by=request.user)
        else:
            init = form.cleaned_data.get("initial_stock")
            if init and init > 0:
                services.add_movement(p, init, "opening", "", request.user, "Початковий залишок")
        if p.auto_price and pricing.reprice_product(p, request.user):
            messages.info(request, "Автоціна: ціну продажу перераховано за правилами цін")
        messages.success(request, f"Товар «{p.brand} {p.sku}» збережено")
        if request.POST.get("again") and old_prices is None:          # швидке додавання серії: бренд і категорія лишаються
            q = {"brand": p.brand, "unit": p.unit}
            if p.category_id:
                q["category"] = p.category_id
            return redirect(reverse("product_new") + "?" + urlencode(q))
        return redirect("product_list")
    history = obj.price_history.select_related("changed_by")[:20] if obj else []
    tiers = [{"id": t.pk, "name": t.name, "pct": float(t.percent)}
             for t in MarkupTier.objects.filter(company=request.company)]
    dt = MarkupTier.objects.filter(company=request.company, is_default=True).first()
    return render(request, "core/product_form.html", {
        "form": form, "obj": obj, "history": history, "brands": all_brands(request), "tiers": tiers,
        "default_pct": float(dt.percent) if dt else 30.0,
        "is_auto_parts": request.company.business_type == "auto_parts"})


@tenant_staff_only
@company_required
def product_lookup(request):
    """Підказки під час введення бренду й артикула: дубль на складі, пропозиції постачальників, дані про деталь."""
    from . import crosses
    from .models import PartInfo, PartOe
    brand = " ".join(request.GET.get("b", "").split()).upper()
    sn = norm_sku(request.GET.get("s", ""))
    if len(sn) < 2:
        return JsonResponse({"empty": True})
    exclude = request.GET.get("pk") or 0
    same = company_queryset(Product.objects.filter(sku_norm=sn), request).exclude(pk=exclude)
    is_auto_parts = request.company.business_type == "auto_parts"
    dup = (same.filter(brand__iexact=brand).first() if brand else same.filter(brand="").first()) if is_auto_parts else same.first()
    others = ([{"brand": p.brand or "—", "name": p.name, "url": reverse("product_edit", args=[p.pk])}
               for p in same.exclude(pk=dup.pk if dup else 0)[:5]] if is_auto_parts else [])
    default_tier = MarkupTier.objects.filter(company=request.company, is_default=True).first()
    pct = default_tier.percent if default_tier else D0
    offers_qs = company_queryset(SupplierItem.objects.filter(sku_norm=sn), request).select_related("supplier").order_by("-available", "cost")
    if brand:
        offers_qs = offers_qs.filter(brand=brand)
    offers = [{"id": o.pk, "brand": o.brand, "sku": o.sku, "name": o.name, "supplier": o.supplier.name, "cost": float(o.cost),
               "qty": float(o.qty_avail) if o.qty_avail is not None else None, "available": o.available,
               "price": float(services.markup_price(o.cost, pct))} for o in offers_qs[:8]]
    ib = brand or (offers[0]["brand"] if offers else "")
    info = PartInfo.objects.filter(brand=ib, sku_norm=sn).first() if is_auto_parts else None
    card = reverse("part_card") + "?" + urlencode({"b": ib, "s": sn})
    data = {"dup": {"name": dup.name, "brand": dup.brand, "sku": dup.sku, "stock": float(dup.current_stock),
                    "url": reverse("product_edit", args=[dup.pk])} if dup else None,
            "others": others, "offers": offers, "card": card}
    if is_auto_parts and (info or PartOe.objects.filter(brand=ib, sku_norm=sn).exists() or offers):
        data["info"] = {"description": (info.description[:260] if info else ""), "image": info.image_url if info else "",
                        "oe": PartOe.objects.filter(brand=ib, sku_norm=sn).count(),
                        "analogs": len(crosses.analogs(ib, sn, limit=50, company=request.company))}
    return JsonResponse(data)


def simple_form(request, model_form, title, back, pk=None, model=None):
    scoped = company_queryset(model.objects.all(), request) if request.company is not None else model.objects.all()
    obj = get_object_or_404(scoped, pk=pk) if pk else None
    form_kwargs = {"instance": obj}
    if model in (Category, Supplier, Customer, PriceRule, PayPlan):
        form_kwargs["company"] = request.company
    form = model_form(request.POST or None, **form_kwargs)
    if request.method == "POST" and form.is_valid():
        record = form.save(commit=False)
        record.company = request.company
        record.save()
        if model is MarkupTier and record.is_default:
            MarkupTier.objects.filter(company=request.company).exclude(pk=record.pk).update(is_default=False)
        form.save_m2m()
        messages.success(request, "Збережено")
        return redirect(back)
    return render(request, "core/form.html", {"form": form, "title": title, "back": back})


def category_list(request):
    return render(request, "core/simple_list.html", {"title": "Категорії", "items": company_queryset(Category.objects.all(), request),
                                                    "add": "category_new", "edit": "category_edit"})


def supplier_list(request):
    qs = company_queryset(Supplier.objects.all(), request)
    q = (request.GET.get("q") or "").strip()[:120]
    active = (request.GET.get("active") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(contact_person__icontains=q) | Q(phone__icontains=q))
    if active == "1":
        qs = qs.filter(is_active=True)
    elif active == "0":
        qs = qs.filter(is_active=False)
    return render(request, "core/simple_list.html", {
        "title": "Постачальники", "items": qs, "add": "supplier_new", "edit": "supplier_edit",
        "q": q, "active": active, "total_count": qs.count(),
    })


category_list = tenant_staff_only(company_required(category_list))
supplier_list = tenant_staff_only(company_required(supplier_list))


@tenant_staff_only
@company_required
def category_edit(request, pk=None):
    return simple_form(request, CategoryForm, "Категорія", "category_list", pk, Category)


@tenant_staff_only
@company_required
def supplier_edit(request, pk=None):
    return simple_form(request, SupplierForm, "Постачальник", "supplier_list", pk, Supplier)


# ---------- Purchases ----------
@login_required
@company_required
@tenant_staff_only
def purchase_list(request):
    qs = company_queryset(Purchase.objects.select_related("supplier"), request)
    q = request.GET.get("q", "").strip()
    supplier_id = request.GET.get("supplier", "").strip()
    d1, d2 = get_date(request, "from", None), get_date(request, "to", None)
    if d1:
        qs = qs.filter(created_at__date__gte=d1)
    if d2:
        qs = qs.filter(created_at__date__lte=d2)
    if supplier_id.isdigit():
        qs = qs.filter(supplier_id=int(supplier_id))
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(supplier__name__icontains=q))
    summary = qs.aggregate(total=Sum("total"), count=Count("pk"))
    from .designer_views import print_bar
    return render(request, "core/purchase_list.html", {
        "purchases": qs[:300], "suppliers": company_queryset(Supplier.objects.all(), request),
        "selected_supplier": supplier_id, "q": q,
        "summary_total": summary["total"] or D0, "summary_count": summary["count"],
        **print_bar("purchase", request.company)})


@login_required
@company_required
@tenant_staff_only
def purchase_recommendations(request):
    """Рекомендації закупівлі за продажами, мінімальним запасом і прайсами."""
    today = timezone.localdate()
    d1 = get_date(request, "from", today - timedelta(days=89))
    d2 = get_date(request, "to", today)
    if d1 > d2:
        d1, d2 = d2, d1
    period_days = max((d2 - d1).days + 1, 1)
    try:
        target_days = int(request.GET.get("target_days", "30"))
    except (TypeError, ValueError):
        target_days = 30
    if target_days not in (14, 30, 45, 60):
        target_days = 30
    products = company_queryset(Product.objects.filter(is_active=True, product_kind="goods").select_related("category"), request).order_by("name")
    sold = _items(d1, d2, request.company).filter(source=SaleItem.STOCK).values("product_id").annotate(qty=Sum(NET))
    sold_by_product = {row["product_id"]: max(D0, row["qty"] or D0) for row in sold}
    pending = PurchaseOrderItem.objects.filter(
        company=request.company, order__status__in=(PurchaseOrder.SENT, PurchaseOrder.CONFIRMED, PurchaseOrder.PARTIAL)
    ).values("product_id").annotate(
        qty=Sum(ExpressionWrapper(F("quantity") - F("received_quantity"),
            output_field=DecimalField(max_digits=14, decimal_places=3)))
    )
    pending_by_product = {row["product_id"]: max(D0, row["qty"] or D0) for row in pending}
    rows, total_units, total_cost = [], D0, D0
    for product in products:
        sold_qty = sold_by_product.get(product.pk, D0)
        daily_qty = sold_qty / Decimal(period_days)
        target_stock = daily_qty * Decimal(target_days) + (product.min_stock or D0)
        ordered_qty = pending_by_product.get(product.pk, D0)
        recommended = max(D0, target_stock - (product.current_stock or D0) - ordered_qty)
        if recommended <= 0 or (daily_qty <= 0 and (product.min_stock or D0) <= (product.current_stock or D0)):
            continue
        recommended = recommended.quantize(Decimal("0.001"), rounding=ROUND_UP)
        offers = company_queryset(SupplierItem.objects.filter(available=True, cost__gt=0).filter(Q(qty_avail__isnull=True) | Q(qty_avail__gt=0)).select_related("supplier"), request).filter(
            Q(product=product) | Q(sku_norm=product.sku_norm, brand__iexact=product.brand)
        ).order_by("cost", "-updated_at")
        offer = offers.first()
        supplier_qty = recommended
        if offer and offer.qty_avail is not None:
            supplier_qty = min(recommended, max(D0, offer.qty_avail))
        unit_cost = offer.cost if offer else product.cost_price
        rows.append({"product": product, "sold_qty": sold_qty, "daily_qty": daily_qty,
            "target_stock": target_stock, "recommended": recommended, "ordered_qty": ordered_qty, "supplier": offer.supplier if offer else None,
            "supplier_item": offer, "supplier_qty": supplier_qty, "unit_cost": unit_cost,
            "estimated_total": supplier_qty * unit_cost})
        total_units += supplier_qty
        total_cost += supplier_qty * unit_cost
    rows.sort(key=lambda row: (row["supplier"] is None, -row["estimated_total"], row["product"].name))
    return render(request, "core/purchase_recommendations.html", {
        "rows": rows[:500], "target_days": target_days, "d1": d1, "d2": d2,
        "period_days": period_days, "total_units": total_units, "total_cost": total_cost,
        "recommendation_count": len(rows)})


@login_required
@company_required
@tenant_staff_only
@require_POST
def purchase_order_create(request):
    product_ids = request.POST.getlist("product")
    quantities = request.POST.getlist("qty")
    offer_ids = request.POST.getlist("offer")
    if not product_ids or not (len(product_ids) == len(quantities) == len(offer_ids)):
        messages.error(request, "Оберіть товари з підтвердженими пропозиціями постачальників.")
        return redirect("purchase_recommendations")
    grouped = {}
    try:
        with transaction.atomic():
            for index, (pid, raw_qty, offer_id) in enumerate(zip(product_ids, quantities, offer_ids), start=1):
                product = company_queryset(Product.objects.all(), request).filter(pk=pid or 0).first()
                qty = dec(raw_qty)
                offer = company_queryset(SupplierItem.objects.select_related("supplier"), request).filter(
                    pk=offer_id or 0, available=True
                ).first()
                if not product or not offer or offer.supplier.company_id != request.company.pk:
                    raise StockError(f"Рядок {index}: товар або пропозиція постачальника недоступні.")
                if offer.product_id and offer.product_id != product.pk:
                    raise StockError(f"Рядок {index}: пропозиція не відповідає вибраному товару.")
                if not offer.product_id and (offer.sku_norm != product.sku_norm or (offer.brand or "").casefold() != (product.brand or "").casefold()):
                    raise StockError(f"Рядок {index}: артикул і бренд пропозиції не збігаються з товаром.")
                if qty is None or qty <= 0:
                    raise StockError(f"Рядок {index}: кількість має бути більшою за нуль.")
                if offer.qty_avail is not None and qty > offer.qty_avail:
                    raise StockError(f"Рядок {index}: у постачальника доступно лише {offer.qty_avail}.")
                grouped.setdefault(offer.supplier_id, []).append((product, qty, offer))
            created = []
            for supplier_id, lines in grouped.items():
                supplier = company_queryset(Supplier.objects.filter(is_active=True), request).get(pk=supplier_id)
                order = PurchaseOrder.objects.create(company=request.company, supplier=supplier, user=request.user)
                order.number = f"PO-{order.pk:06d}"
                order.save(update_fields=["number"])
                for product, qty, offer in lines:
                    PurchaseOrderItem.objects.create(
                        company=request.company, order=order, product=product,
                        supplier_item=offer, quantity=qty, cost_price=offer.cost,
                    )
                created.append(order)
        messages.success(request, f"Створено замовлень постачальникам: {len(created)}.")
        return redirect("purchase_order_list")
    except (StockError, Supplier.DoesNotExist) as exc:
        messages.error(request, str(exc))
        return redirect("purchase_recommendations")


@login_required
@company_required
@tenant_staff_only
def purchase_order_list(request):
    orders = company_queryset(PurchaseOrder.objects.select_related("supplier", "user"), request)
    status = request.GET.get("status", "").strip()
    if status in dict(PurchaseOrder.STATUS_CHOICES):
        orders = orders.filter(status=status)
    return render(request, "core/purchase_order_list.html", {
        "orders": orders[:300], "statuses": PurchaseOrder.STATUS_CHOICES, "selected_status": status,
    })


@login_required
@company_required
@tenant_staff_only
def purchase_order_control(request):
    today = timezone.localdate()
    active_statuses = (PurchaseOrder.SENT, PurchaseOrder.CONFIRMED, PurchaseOrder.PARTIAL)
    orders = company_queryset(
        PurchaseOrder.objects.select_related("supplier", "user").prefetch_related("items__product"),
        request,
    ).filter(status__in=active_statuses)
    filter_mode = request.GET.get("delivery", "all").strip()
    if filter_mode == "overdue":
        orders = orders.filter(expected_date__lt=today)
    elif filter_mode == "incomplete":
        orders = orders.filter(items__received_quantity__lt=F("items__quantity")).distinct()

    rows = []
    overdue_count = 0
    incomplete_count = 0
    outstanding_units = D0
    outstanding_value = D0
    for order in orders.order_by("expected_date", "created_at")[:500]:
        lines = []
        order_qty = D0
        order_value = D0
        for item in order.items.all():
            remaining = item.outstanding_quantity
            if remaining <= 0:
                continue
            order_qty += remaining
            order_value += remaining * item.cost_price
            lines.append({
                "item": item, "remaining": remaining,
                "remaining_value": remaining * item.cost_price,
            })
        if not lines:
            continue
        order.is_overdue = bool(order.expected_date and order.expected_date < today)
        order.outstanding_lines = lines
        order.outstanding_quantity = order_qty
        order.outstanding_value = order_value
        order.days_overdue = (today - order.expected_date).days if order.is_overdue else 0
        overdue_count += int(order.is_overdue)
        incomplete_count += 1
        outstanding_units += order_qty
        outstanding_value += order_value
        rows.append(order)

    return render(request, "core/purchase_order_control.html", {
        "orders": rows, "today": today, "selected_delivery": filter_mode,
        "overdue_count": overdue_count, "incomplete_count": incomplete_count,
        "outstanding_units": outstanding_units, "outstanding_value": outstanding_value,
    })


@login_required
@company_required
@tenant_staff_only
def supplier_analytics(request):
    """Порівняння пропозицій і фактична якість постачань у межах поточної компанії."""
    today = timezone.localdate()
    default_from = today - timedelta(days=90)
    try:
        date_from = date.fromisoformat(request.GET.get("from", "").strip()) if request.GET.get("from") else default_from
        date_to = date.fromisoformat(request.GET.get("to", "").strip()) if request.GET.get("to") else today
        if date_from > date_to:
            raise ValueError
    except ValueError:
        date_from, date_to = default_from, today
        messages.error(request, "Перевірте період звіту: дата початку має бути не пізніше дати завершення.")
    supplier_id = request.GET.get("supplier", "").strip()
    query = request.GET.get("q", "").strip()
    receipt_items = PurchaseItem.objects.select_related("purchase")
    item_query = PurchaseOrderItem.objects.select_related("product").prefetch_related(
        Prefetch("receipt_items", queryset=receipt_items)
    )
    orders = company_queryset(
        PurchaseOrder.objects.select_related("supplier").prefetch_related(Prefetch("items", queryset=item_query)),
        request,
    ).filter(created_at__date__gte=date_from, created_at__date__lte=date_to).exclude(
        status__in=(PurchaseOrder.DRAFT, PurchaseOrder.CANCELLED)
    )
    if supplier_id.isdigit():
        orders = orders.filter(supplier_id=int(supplier_id))
    stats = {}
    for order in orders.order_by("supplier__name", "-created_at")[:2000]:
        row = stats.setdefault(order.supplier_id, {
            "supplier": order.supplier, "orders": 0, "completed": 0, "on_time": 0,
            "orders_with_due_date": 0, "ordered_qty": D0, "received_qty": D0,
            "planned_value": D0, "planned_received_value": D0, "actual_value": D0, "price_qty": D0,
        })
        row["orders"] += 1
        order_ordered = D0
        order_received = D0
        latest_receipt = None
        for item in order.items.all():
            order_ordered += item.quantity
            row["ordered_qty"] += item.quantity
            row["planned_value"] += item.quantity * item.cost_price
            for receipt in item.receipt_items.all():
                order_received += receipt.quantity
                row["received_qty"] += receipt.quantity
                row["planned_received_value"] += receipt.quantity * item.cost_price
                row["actual_value"] += receipt.quantity * receipt.cost_price
                row["price_qty"] += receipt.quantity
                receipt_date = timezone.localtime(receipt.purchase.created_at).date()
                latest_receipt = max(latest_receipt, receipt_date) if latest_receipt else receipt_date
        if order_ordered > 0 and order_received >= order_ordered:
            row["completed"] += 1
            if order.expected_date:
                row["orders_with_due_date"] += 1
                if latest_receipt and latest_receipt <= order.expected_date:
                    row["on_time"] += 1
    performance = []
    for row in stats.values():
        row["fill_rate"] = (
            (row["received_qty"] / row["ordered_qty"] * Decimal("100")).quantize(Decimal("0.1"))
            if row["ordered_qty"] else D0
        )
        row["on_time_rate"] = (
            (Decimal(row["on_time"]) / row["orders_with_due_date"] * Decimal("100")).quantize(Decimal("0.1"))
            if row["orders_with_due_date"] else None
        )
        row["price_delta"] = row["actual_value"] - row["planned_received_value"]
        row["avg_actual_cost"] = (
            (row["actual_value"] / row["price_qty"]).quantize(Decimal("0.01")) if row["price_qty"] else None
        )
        performance.append(row)
    performance.sort(key=lambda x: (
        x["on_time_rate"] is None, -(x["on_time_rate"] or D0), -x["fill_rate"], x["supplier"].name
    ))
    offers = company_queryset(SupplierItem.objects.select_related("supplier", "product"), request).filter(
        available=True, cost__gt=0, product__isnull=False, supplier__is_active=True
    )
    if query:
        offers = offers.filter(
            Q(product__name__icontains=query) | Q(product__sku__icontains=query)
            | Q(brand__icontains=query) | Q(sku__icontains=query)
        )
    grouped = {}
    for offer in offers.order_by("product__name", "cost", "supplier__name")[:5000]:
        group = grouped.setdefault(offer.product_id, {"product": offer.product, "offers": []})
        group["offers"].append(offer)
    comparisons = [group for group in grouped.values() if len(group["offers"]) >= 2]
    for group in comparisons:
        minimum = min(offer.cost for offer in group["offers"])
        for offer in group["offers"]:
            offer.delta_to_min = offer.cost - minimum
            offer.is_cheapest = offer.cost == minimum
    comparisons.sort(key=lambda x: x["product"].name.lower())
    return render(request, "core/supplier_analytics.html", {
        "performance": performance, "comparisons": comparisons[:200],
        "suppliers": company_queryset(Supplier.objects.filter(is_active=True).order_by("name"), request),
        "selected_supplier": supplier_id, "query": query, "date_from": date_from, "date_to": date_to,
    })


@login_required
@company_required
@tenant_staff_only
def purchase_order_detail(request, pk):
    order = get_object_or_404(
        company_queryset(PurchaseOrder.objects.select_related("supplier", "user"), request), pk=pk
    )
    items = list(order.items.select_related("product", "supplier_item").all())
    for item in items:
        item.receipt_price_rows = [
            {
                "date": receipt.purchase.created_at,
                "quantity": receipt.quantity,
                "actual_cost": receipt.cost_price,
                "delta": receipt.cost_price - item.cost_price,
            }
            for receipt in item.receipt_items.select_related("purchase").all()
        ]
    return render(request, "core/purchase_order_detail.html", {"order": order, "items": items})


@login_required
@company_required
@tenant_staff_only
def purchase_order_print(request, pk):
    order = get_object_or_404(
        company_queryset(PurchaseOrder.objects.select_related("supplier", "user"), request), pk=pk
    )
    items = list(order.items.select_related("product").all())
    total = sum((item.line_total for item in items), D0)
    return render(request, "core/purchase_order_print.html", {
        "order": order, "items": items, "total": total,
    })


@login_required
@company_required
@tenant_staff_only
def purchase_order_export_csv(request, pk):
    order = get_object_or_404(
        company_queryset(PurchaseOrder.objects.select_related("supplier"), request), pk=pk
    )
    items = list(order.items.select_related("product").all())
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="purchase-order-{order.number or order.pk}.csv"'
    writer = csv.writer(response, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    def safe_cell(value):
        value = "" if value is None else str(value)
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

    writer.writerow(["Замовлення постачальнику", safe_cell(order.number)])
    writer.writerow(["Постачальник", safe_cell(order.supplier.name)])
    writer.writerow(["Контактна особа", safe_cell(order.supplier.contact_person)])
    writer.writerow(["Телефон", safe_cell(order.supplier.phone)])
    writer.writerow(["Дата", timezone.localtime(order.created_at).strftime("%d.%m.%Y %H:%M")])
    writer.writerow(["Очікувана доставка", order.expected_date.strftime("%d.%m.%Y") if order.expected_date else ""])
    writer.writerow(["Статус", order.get_status_display()])
    writer.writerow(["Примітка", safe_cell(order.note)])
    writer.writerow([])
    writer.writerow([
        "№", "Товар", "Бренд", "Артикул", "Од. виміру", "Замовлено",
        "Отримано", "Залишок до отримання", "Ціна планова", "Сума планова",
    ])
    total = D0
    for index, item in enumerate(items, start=1):
        line_total = item.line_total
        total += line_total
        writer.writerow([
            index, safe_cell(item.product.name), safe_cell(item.product.brand),
            safe_cell(item.product.sku), safe_cell(item.product.unit),
            str(item.quantity), str(item.received_quantity), str(item.outstanding_quantity),
            str(item.cost_price), str(line_total),
        ])
    writer.writerow([])
    writer.writerow(["", "", "", "", "", "", "", "Разом:", "", str(total)])
    return response


@login_required
@company_required
@tenant_staff_only
@require_POST
def purchase_order_update(request, pk):
    try:
        with transaction.atomic():
            order = get_object_or_404(
                company_queryset(PurchaseOrder.objects.select_for_update(), request), pk=pk
            )
            if order.status != PurchaseOrder.DRAFT:
                raise StockError("Редагувати можна лише замовлення у статусі «Чернетка».")
            raw_date = request.POST.get("expected_date", "").strip()
            expected_date = datetime.strptime(raw_date, "%Y-%m-%d").date() if raw_date else None
            note = request.POST.get("note", "").strip()
            if len(note) > 10000:
                raise StockError("Примітка не може перевищувати 10 000 символів.")
            order.expected_date = expected_date
            order.note = note
            order.save(update_fields=["expected_date", "note", "updated_at"])
        messages.success(request, "Дату очікування та примітку збережено.")
    except (StockError, ValueError) as exc:
        messages.error(request, str(exc) or "Перевірте дату очікування.")
    return redirect("purchase_order_detail", pk=pk)


@login_required
@company_required
@tenant_staff_only
@require_POST
def purchase_order_status(request, pk, action):
    with transaction.atomic():
        order = get_object_or_404(company_queryset(PurchaseOrder.objects.select_for_update(), request), pk=pk)
        now = timezone.now()
        if action == "send" and order.status == PurchaseOrder.DRAFT:
            order.status, order.sent_at = PurchaseOrder.SENT, now
        elif action == "confirm" and order.status == PurchaseOrder.SENT:
            order.status, order.confirmed_at = PurchaseOrder.CONFIRMED, now
        elif action == "cancel" and order.status in (PurchaseOrder.DRAFT, PurchaseOrder.SENT, PurchaseOrder.CONFIRMED):
            if order.items.filter(received_quantity__gt=0).exists():
                messages.error(request, "Замовлення з уже отриманими товарами не можна скасувати.")
                return redirect("purchase_order_detail", pk=order.pk)
            order.status, order.closed_at = PurchaseOrder.CANCELLED, now
        else:
            messages.error(request, "Ця зміна статусу зараз недоступна.")
            return redirect("purchase_order_detail", pk=order.pk)
        order.save(update_fields=["status", "sent_at", "confirmed_at", "closed_at", "updated_at"])
    messages.success(request, "Статус замовлення оновлено.")
    return redirect("purchase_order_detail", pk=pk)


@login_required
@company_required
@tenant_staff_only
@require_POST
def purchase_order_receive(request, pk, item_pk):
    try:
        with transaction.atomic():
            order = get_object_or_404(
                company_queryset(PurchaseOrder.objects.select_for_update().select_related("supplier"), request), pk=pk
            )
            if order.status not in (PurchaseOrder.CONFIRMED, PurchaseOrder.PARTIAL):
                raise StockError("Спочатку підтвердіть замовлення постачальнику.")
            item = get_object_or_404(
                PurchaseOrderItem.objects.select_for_update().select_related("product"),
                pk=item_pk, order=order, company=request.company,
            )
            qty = dec(request.POST.get("quantity"))
            if qty is None or qty <= 0 or qty > item.outstanding_quantity:
                raise StockError(f"Вкажіть кількість від 0.001 до {item.outstanding_quantity}.")
            actual_cost = dec(request.POST.get("actual_cost"), item.cost_price)
            if actual_cost is None or actual_cost < 0:
                raise StockError("Фактична закупівельна ціна має бути невід'ємним числом.")
            raw_expiry = request.POST.get("expiry_date", "").strip()
            expiry = datetime.strptime(raw_expiry, "%Y-%m-%d").date() if raw_expiry else None
            batch_number = request.POST.get("batch_number", "").strip()
            serials = request.POST.get("serial_numbers", "").splitlines()
            product = item.product
            if product.tracking_mode == "expiry" and not expiry:
                raise StockError("Для цього товару потрібно вказати термін придатності.")
            if product.tracking_mode == "serial" and not any(x.strip() for x in serials):
                raise StockError("Для серійного товару введіть серійні номери.")
            if product.tracking_mode not in ("batch", "expiry") and (batch_number or expiry):
                raise StockError("Цей товар не використовує партійний облік.")
            if product.tracking_mode != "serial" and any(x.strip() for x in serials):
                raise StockError("Цей товар не використовує серійний облік.")
            receipt = services.create_purchase(
                request.user, order.supplier,
                [(product, qty, actual_cost, batch_number, expiry, serials)],
                update_cost=False, company=request.company,
            )
            receipt_item = receipt.items.get(product=product)
            receipt_item.order_item = item
            receipt_item.save(update_fields=["order_item"])
            item.received_quantity += qty
            item.save(update_fields=["received_quantity"])
            remaining = order.items.filter(received_quantity__lt=F("quantity")).exists()
            if remaining:
                order.status = PurchaseOrder.PARTIAL
            else:
                order.status, order.closed_at = PurchaseOrder.RECEIVED, timezone.now()
            order.save(update_fields=["status", "closed_at", "updated_at"])
        messages.success(request, f"Створено накладну надходження {receipt.number}; отримано {qty} за фактичною ціною {actual_cost}.")
    except (StockError, ValueError) as exc:
        messages.error(request, str(exc) or "Перевірте дату придатності.")
    return redirect("purchase_order_detail", pk=pk)


@login_required
@company_required
@tenant_staff_only
def purchase_new(request):
    if request.method == "POST":
        sid, new_name = request.POST.get("supplier"), request.POST.get("new_supplier", "").strip()
        lines = []
        product_ids = request.POST.getlist("product")
        quantities = request.POST.getlist("qty")
        costs = request.POST.getlist("cost")
        batch_numbers = request.POST.getlist("batch_number")
        expiry_dates = request.POST.getlist("expiry_date")
        serial_values = request.POST.getlist("serial_numbers")
        try:
            if not (len(product_ids) == len(quantities) == len(costs) == len(batch_numbers) == len(expiry_dates) == len(serial_values)):
                raise StockError("Дані рядків закупівлі неповні. Оновіть сторінку й повторіть.")
            for index, (pid, raw_q, raw_cost, batch_number, raw_expiry, raw_serials) in enumerate(zip(product_ids, quantities, costs, batch_numbers, expiry_dates, serial_values), start=1):
                # Empty extra rows are normal in the form; partially filled rows are not.
                if not any(str(value or "").strip() for value in (pid, raw_q, raw_cost, batch_number, raw_expiry, raw_serials)):
                    continue
                p = company_queryset(Product.objects.all(), request).filter(pk=pid or 0).first()
                q, cost = dec(raw_q), dec(raw_cost)
                if p is None:
                    raise StockError(f"Рядок {index}: оберіть товар зі списку.")
                if q is None or q <= 0:
                    raise StockError(f"Рядок {index}: кількість має бути більшою за нуль.")
                if cost is None or cost < 0:
                    raise StockError(f"Рядок {index}: закупівельна ціна не може бути від'ємною.")
                expiry = None
                if raw_expiry.strip():
                    try:
                        expiry = datetime.strptime(raw_expiry.strip(), "%Y-%m-%d").date()
                    except ValueError:
                        raise StockError(f"Рядок {index}: некоректна дата придатності.")
                lines.append((p, q, cost, batch_number.strip(), expiry, raw_serials.splitlines()))
            if not lines:
                raise StockError("Додайте хоча б один товар")
            if new_name and sid:
                raise StockError("Оберіть наявного постачальника або введіть назву нового, не обидва варіанти.")
            with transaction.atomic():
                supplier = Supplier.objects.create(company=request.company, name=new_name) if new_name else company_queryset(Supplier.objects.filter(is_active=True), request).filter(pk=sid or 0).first()
                if not supplier:
                    raise StockError("Оберіть активного постачальника або введіть назву нового.")
                pur = services.create_purchase(request.user, supplier, lines, bool(request.POST.get("update_cost")), company=request.company)
            messages.success(request, f"Накладну {pur.number} проведено")
            return redirect("purchase_detail", pk=pur.pk)
        except StockError as e:
            messages.error(request, str(e))
    initial_lines = []
    product_ids, quantities, costs = request.GET.getlist("product"), request.GET.getlist("qty"), request.GET.getlist("cost")
    if len(product_ids) == len(quantities) == len(costs):
        for pid, raw_qty, raw_cost in zip(product_ids, quantities, costs):
            product = company_queryset(Product.objects.filter(is_active=True), request).filter(pk=pid or 0).first()
            qty, cost = dec(raw_qty), dec(raw_cost)
            if product and qty is not None and qty > 0 and cost is not None and cost >= 0:
                initial_lines.append({"product_id": product.pk, "quantity": str(qty), "cost": str(cost)})
    return render(request, "core/purchase_form.html", {
        "suppliers": company_queryset(Supplier.objects.filter(is_active=True), request),
        "products": company_queryset(Product.objects.filter(is_active=True), request),
        "initial_lines": initial_lines})


@login_required
@company_required
@tenant_staff_only
def purchase_detail(request, pk):
    purchase = get_object_or_404(company_queryset(Purchase.objects.select_related("supplier"), request), pk=pk)
    payments = purchase.settlement_payments.filter(company=request.company, is_void=False)
    paid = payments.aggregate(total=Sum("amount"))["total"] or D0
    from .models import AccountAdvance
    available_advances = AccountAdvance.objects.filter(
        company=request.company, party_type=AccountAdvance.SUPPLIER, supplier_id=purchase.supplier_id,
        is_void=False, remaining__gt=0,
    )
    return render(request, "core/purchase_detail.html", {
        "p": purchase, "settlement_payments": purchase.settlement_payments.select_related("created_by", "voided_by", "advance"),
        "settlement_paid": paid, "settlement_balance": max(D0, purchase.total - paid),
        "available_advances": available_advances,
    })


# ---------- POS ----------
@tenant_cashier_only
def pos(request):
    shift = current_shift(request.user, request.company)
    if not shift and not request.user.is_staff:
        messages.info(request, "Спочатку відкрийте зміну")
        return redirect("shift")
    init = {"cart": [], "customer": None, "resume_id": None}
    rid = request.GET.get("resume")
    if rid:
        held = get_object_or_404(company_queryset(Sale.objects.all(), request), pk=rid, status=Sale.HELD)
        if held.cashier_id != request.user.id and not _can_manage_company(request):
            raise PermissionDenied
        init["resume_id"] = held.pk
        init["payment"] = held.payment_method
        if held.customer:
            init["customer"] = _customer_json(held.customer)
            init["car"] = _car_json(held.car) if held.car_id else None
        init["cart"] = [_cart_line_json(i) for i in held.items.select_related("product", "supplier_item__supplier")]
    cats = {c.pk: c for c in company_queryset(Category.objects.all(), request)}

    def root(cid):
        while cid and cats[cid].parent_id:
            cid = cats[cid].parent_id
        return cid
    products = [{"id": p.pk, "n": p.name, "s": p.sku, "b": p.barcode or "", "p": float(p.price), "a": p.allow_price_change,
                 "st": float(p.current_stock), "c": root(p.category_id), "f": p.is_favorite, "u": p.unit,
                 "k": float(p.cost_price), "br": p.brand, "tm": p.tracking_mode}
                for p in company_queryset(Product.objects.filter(is_active=True).exclude(from_supplier=True, current_stock__lte=0), request)]
    init["products"] = products
    init["tiers"] = [{"id": t.pk, "name": t.name, "pct": float(t.percent)} for t in company_queryset(MarkupTier.objects.all(), request)]
    dt = company_queryset(MarkupTier.objects.filter(is_default=True), request).first()
    init["default_pct"] = float(dt.percent) if dt else 0
    init["cats"] = [{"id": c.pk, "name": c.name} for c in cats.values() if not c.parent_id]
    init["denoms"] = [{"k": d, "l": cashlib.label(d)} for d in cashlib.DENOMS]
    init["drawer"] = float(cashlib.to_dec(cashlib.total(shift.drawer))) if shift else None
    cs = CompanySettings.get(request.company)
    init["auto_print"], init["auto_print_format"] = cs.auto_print, cs.auto_print_format
    return render(request, "core/pos.html", {"init": init, "shift": shift})


def _car_json(car):
    return {"id": car.pk, "title": car.title, "plate": car.plate, "vin": car.vin, "make": car.make, "model": car.model,
            "year": car.year}


def _customer_json(c):
    return {"id": c.pk, "name": c.name, "pct": float(services.tier_pct(c, company=c.company)), "tier": c.markup_tier_id,
            "cars": [_car_json(x) for x in c.cars.all()[:20]]}


def _cart_line_json(i):
    base = {"id": i.product_id, "name": i.product.name, "price": float(i.price), "orig": float(i.original_price),
            "qty": float(i.quantity), "unit": i.product.unit, "cost": float(i.cost_price)}
    si = i.supplier_item if i.source == SaleItem.SUPPLIER else None
    if si:
        return dict(base, key=f"s{si.pk}", si=si.pk, allow=True, name=f"{si.brand} {si.sku} {si.name}".strip(),
                    rl=pricing.rule_json(pricing.RuleSet(company=i.product.company).for_(si.brand, i.product.category_id)),
                    sup=si.supplier.name, avail=float(si.qty_avail) if si.qty_avail is not None else None)
    return dict(base, key=f"p{i.product_id}", allow=i.product.allow_price_change,
                name=f"{i.product.brand} {i.product.name}".strip(), tm=i.product.tracking_mode, serial_number="")


def product_json(p):
    return {"id": p.pk, "name": p.name, "sku": p.sku, "barcode": p.barcode or "", "price": float(p.price),
            "allow": p.allow_price_change, "stock": float(p.current_stock), "unit": p.unit}


@tenant_cashier_only
def pos_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"results": [], "exact": False})
    qs = company_queryset(Product.objects.filter(is_active=True), request)
    exact = qs.filter(Q(barcode=q) | Q(sku__iexact=q)).first()
    if exact is None:
        # Use a company-scoped subquery for additional barcodes.
        extra_product_ids = ProductBarcode.objects.filter(
            company=request.company, barcode=q
        ).values("product_id")
        exact = qs.filter(pk__in=extra_product_ids).first()
    if exact:
        return JsonResponse({"results": [product_json(exact)], "exact": True})
    extra_product_ids = ProductBarcode.objects.filter(
        company=request.company, barcode__icontains=q
    ).values("product_id")
    res = qs.filter(
        Q(name_lc__contains=q.lower()) | Q(sku__icontains=q) | Q(barcode__icontains=q)
        | Q(pk__in=extra_product_ids)
    )[:15]
    return JsonResponse({"results": [product_json(p) for p in res], "exact": False})


@tenant_cashier_only
def pos_customer(request):
    q = request.GET.get("q", "").strip()
    cond = Q(name_lc__contains=q.lower()) | Q(phone__icontains=q)
    pn = norm_plate(q)
    if len(pn) >= 3:                                   # пошук за держномером або VIN авто
        cond |= Q(cars__plate_norm__contains=pn)
    if len(q) >= 5:
        cond |= Q(cars__vin__icontains=q)
    res = company_queryset(Customer.objects.filter(cond), request).distinct().prefetch_related("cars")[:8] if q else []
    return JsonResponse({"results": [dict(_customer_json(c), phone=c.phone) for c in res]})


@tenant_cashier_only
@require_POST
def pos_customer_new(request):
    d = _jb(request)
    name, phone = d.get("name", "").strip(), d.get("phone", "").strip()
    if not name or not phone:
        return JsonResponse({"error": "Потрібні ім'я і телефон"}, status=400)
    c, _ = Customer.objects.get_or_create(company=request.company, phone=phone, defaults={"name": name})
    return JsonResponse(_customer_json(c))


def _parse_cart(request):
    d = _jb(request)
    lines = []
    customer = company_queryset(Customer.objects.select_related("markup_tier"), request).filter(pk=d.get("customer_id") or 0).first()
    for it in d.get("items", []):
        q = dec(it.get("qty"))
        if it.get("si"):                       # позиція з прайсу постачальника: ціна = закупівельна + націнка
            si = company_queryset(SupplierItem.objects.select_related("supplier"), request).filter(pk=it["si"], available=True).first()
            if not si or not q or q <= 0:
                raise StockError("Позиції немає в актуальному прайсі постачальника")
            p = services.ensure_product(si)
            listp = pricing.list_price(si.cost, si.brand, p.category_id, customer, rs=pricing.RuleSet(company=request.company))[0]
            price = dec(it.get("price"), listp)
            if price is None or price < 0:
                price = listp
            lines.append((p, q, price, {"si": si, "list": listp}))
            continue
        p = company_queryset(Product.objects.filter(is_active=True), request).filter(pk=it.get("id")).first()
        if not p or not q or q <= 0:
            raise StockError("Некоректний рядок чека")
        price = p.price
        if p.allow_price_change:
            price = dec(it.get("price"), p.price)
            if price < 0:
                price = p.price
        lines.append((p, q, price, {"serial_number": str(it.get("serial_number") or "").strip()} if p.tracking_mode == "serial" else None))
    pay = d.get("payment") if d.get("payment") in ("cash", "card", "mixed", "credit") else "cash"
    held = None
    if d.get("resume_id"):
        held = company_queryset(Sale.objects.filter(status=Sale.HELD), request).filter(pk=d["resume_id"]).first()
        if held is None:
            raise StockError("Чернетка вже проведена або видалена. Оновіть касу та створіть чек заново.")
        if held.cashier_id != request.user.id and not _can_manage_company(request):
            raise PermissionDenied
    car = None
    if customer and d.get("car_id"):
        car = CustomerCar.objects.filter(pk=d["car_id"], customer=customer, company=request.company).first()
    return lines, pay, customer, held, car


def _payment_inputs(d):
    def kop(x):
        v = dec(x)
        return cashlib.to_kop(v) if v is not None else None
    return d.get("payment") if d.get("payment") in ("cash", "card", "mixed", "credit") else "cash", \
        kop(d.get("card")) or 0, kop(d.get("received")), d.get("counts") or {}


def _plan_json(res):
    return {"ok": res["ok"], "msg": res["msg"], "cash_due": float(cashlib.to_dec(res["cash_due"])),
            "change": float(cashlib.to_dec(res["change"])), "rounding": float(cashlib.to_dec(res["rounding"])),
            "plan": [{"l": cashlib.label(k), "n": n} for k, n in sorted(res["plan"].items(), reverse=True)],
            "drawer_after": (float(cashlib.to_dec(cashlib.total(res["drawer_after"])))
                             if res["drawer_after"] is not None else None)}


def _clean_car(d):
    """Перевіряє й нормалізує дані авто. Повертає (поля, помилка)."""
    vin = re.sub(r"\s+", "", str(d.get("vin") or "")).upper()
    if vin and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return None, "VIN має містити 17 символів: цифри й латинські літери без I, O, Q"
    plate = " ".join(str(d.get("plate") or "").split()).upper()[:20]
    year = d.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        return None, "Рік випуску вказано некоректно"
    if year is not None and not 1950 <= year <= timezone.localdate().year + 1:
        return None, "Рік випуску поза допустимим діапазоном"
    f = {"plate": plate, "vin": vin, "make": str(d.get("make") or "").strip()[:40], "model": str(d.get("model") or "").strip()[:60],
         "year": year, "engine": str(d.get("engine") or "").strip()[:60]}
    if not (f["plate"] or f["vin"] or f["make"] or f["model"]):
        return None, "Вкажіть хоча б держномер, VIN або марку/модель"
    return f, None


@tenant_cashier_only
@require_POST
def pos_car_new(request):
    d = _jb(request)
    cust = get_object_or_404(company_queryset(Customer.objects.all(), request), pk=d.get("customer_id") or 0)
    f, err = _clean_car(d)
    if err:
        return JsonResponse({"error": err}, status=400)
    car = CustomerCar.objects.create(company=request.company, customer=cust, **f)
    return JsonResponse(_car_json(car))


@login_required
@company_required
@require_POST
def car_add(request, pk):
    cust = get_object_or_404(company_queryset(Customer.objects.all(), request), pk=pk)
    f, err = _clean_car(request.POST)
    if err:
        messages.error(request, err)
    else:
        CustomerCar.objects.create(company=request.company, customer=cust, **f)
        messages.success(request, "Авто додано")
    return redirect("customer_detail", pk=pk) if _can_manage_company(request) else redirect("pos")


@login_required
@company_required
@tenant_staff_only
@require_POST
def car_delete(request, pk):
    car = get_object_or_404(CustomerCar.objects.filter(company=request.company), pk=pk)
    cid = car.customer_id
    car.delete()
    messages.success(request, "Авто видалено")
    return redirect("customer_detail", pk=cid)


@tenant_cashier_only
@require_POST
def pos_change(request):
    d = _jb(request)
    shift = current_shift(request.user, request.company)
    pay, card, received, counts = _payment_inputs(d)
    total_kop = cashlib.to_kop(dec(d.get("total"), D0))
    res = cashlib.compute_payment(total_kop, pay, card, received, counts, cashlib.clean(shift.drawer) if shift else None)
    return JsonResponse(_plan_json(res))


@tenant_cashier_only
@require_POST
@transaction.atomic
def pos_checkout(request):
    # Serialize checkouts in the same cash shift before reading/updating its drawer.
    shift = Shift.objects.select_for_update().filter(company=request.company, cashier=request.user, closed_at__isnull=True).first()
    if not shift:
        return JsonResponse({"error": "Зміну не відкрито"}, status=400)
    try:
        lines, pay, customer, held, car = _parse_cart(request)
        merged = services.merge_lines(lines)
        total_kop = cashlib.to_kop(sum((q * pr for _, q, pr, _x in merged), D0))
        pm, card, received, counts = _payment_inputs(_jb(request))
        res = cashlib.compute_payment(total_kop, pm, card, received, counts,
                                      cashlib.clean(shift.drawer) if shift else None)
        if not res["ok"]:
            raise StockError(res["msg"])
        info = {"card": cashlib.to_dec(res["card"]), "received": cashlib.to_dec(res["received"]),
                "change": cashlib.to_dec(res["change"]), "drawer": res["drawer_after"],
                "detail": {"plan": {cashlib.label(k): n for k, n in res["plan"].items()},
                           "rounding": float(cashlib.to_dec(res["rounding"]))}}
        sale = services.finalize_sale(request.user, lines, pm, customer, shift, held, info, car=car, company=request.company)
    except StockError as e:
        return JsonResponse({"error": str(e)}, status=400)
    shift = current_shift(request.user, request.company)
    return JsonResponse({"ok": True, "id": sale.pk, "number": sale.number, "total": float(sale.total),
                         "change": float(sale.change_given), "plan": _plan_json(res)["plan"],
                         "drawer": float(cashlib.to_dec(cashlib.total(shift.drawer))) if shift else None})


@tenant_cashier_only
@require_POST
def pos_hold(request):
    try:
        lines, pay, customer, held, car = _parse_cart(request)
        if not lines:
            raise StockError("Чек порожній")
        sale = services.save_held(request.user, lines, pay, customer, current_shift(request.user, request.company), held, car=car, company=request.company)
    except StockError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"ok": True, "number": sale.number})


@tenant_cashier_only
def held_list(request):
    qs = company_queryset(Sale.objects.filter(status=Sale.HELD).select_related("customer"), request)
    if not _can_manage_company(request):
        qs = qs.filter(cashier=request.user)
    return render(request, "core/held_list.html", {"sales": qs})


@tenant_cashier_only
@require_POST
def held_delete(request, pk):
    s = get_object_or_404(company_queryset(Sale.objects.all(), request), pk=pk, status=Sale.HELD)
    if s.cashier_id != request.user.id and not _can_manage_company(request):
        raise PermissionDenied
    s.delete()
    return redirect("held_list")


# ---------- Sales ----------
@tenant_cashier_only
def sale_list(request):
    qs = done_sales(request.company).select_related("cashier", "customer")
    if not _can_manage_company(request):
        qs = qs.filter(cashier=request.user)
    d1, d2 = get_date(request, "from", None), get_date(request, "to", None)
    if d1:
        qs = qs.filter(created_at__date__gte=d1)
    if d2:
        qs = qs.filter(created_at__date__lte=d2)
    if request.GET.get("payment"):
        qs = qs.filter(payment_method=request.GET["payment"])
    if _can_manage_company(request) and request.GET.get("cashier"):
        qs = qs.filter(cashier_id=request.GET["cashier"])
    from django.contrib.auth import get_user_model
    from .designer_views import print_bar
    return render(request, "core/sale_list.html", {
        "sales": qs.annotate(qty=Sum("items__quantity"))[:300],
        "cashiers": get_user_model().objects.filter(staff_memberships__company=request.company, staff_memberships__is_active=True).distinct(), "payments": Sale.PAYMENTS, **print_bar("receipt", request.company)})


@tenant_cashier_only
def sale_detail(request, pk):
    sale = get_object_or_404(company_queryset(Sale.objects.select_related("cashier", "customer"), request), pk=pk)
    if not _can_manage_company(request) and sale.cashier_id != request.user.id:
        raise PermissionDenied
    returns = sale.returns.filter(company=request.company)
    returned = returns.aggregate(total=Sum("total"))["total"] or D0
    settlement_payments = sale.settlement_payments.filter(company=request.company)
    paid_later = settlement_payments.filter(is_void=False).aggregate(total=Sum("amount"))["total"] or D0
    paid_at_checkout = sale.cash_amount + sale.card_amount
    balance = max(D0, sale.total - returned - paid_at_checkout - paid_later)
    from .models import AccountAdvance
    available_advances = AccountAdvance.objects.filter(
        company=request.company, party_type=AccountAdvance.CUSTOMER, customer_id=sale.customer_id,
        is_void=False, remaining__gt=0,
    ) if sale.customer_id else AccountAdvance.objects.none()
    return render(request, "core/sale_detail.html", {
        "sale": sale, "items": sale.items.filter(company=request.company).select_related("product"),
        "returns": returns, "settlement_payments": settlement_payments.select_related("created_by", "voided_by", "advance"),
        "settlement_paid": paid_later, "settlement_balance": balance,
        "available_advances": available_advances,
    })


@login_required
@company_required
@tenant_staff_only
@require_POST
@transaction.atomic
def sale_return(request, pk):
    sale = get_object_or_404(company_queryset(Sale.objects.all(), request), pk=pk, status=Sale.DONE)
    qty_map = {}
    for item in sale.items.filter(company=request.company):
        q = dec(request.POST.get(f"ret_{item.pk}", "0"), D0)
        if q and q > 0:
            qty_map[item.pk] = q
    try:
        ret = services.create_return(
            request.user, sale, qty_map,
            refund_method=request.POST.get("refund_method", "card"),
            refund_cash_amount=dec(request.POST.get("refund_cash_amount"), None),
            shift=current_shift(request.user, request.company), company=request.company,
        )
        messages.success(
            request,
            f"Повернення {ret.number} на суму {ret.total} ₴ проведено. "
            f"Готівка: {ret.cash_amount} ₴, картка: {ret.card_amount} ₴",
        )
    except StockError as e:
        messages.error(request, str(e))
    return redirect("sale_detail", pk=pk)



@login_required
@company_required
@tenant_staff_only
@require_POST
@transaction.atomic
def sale_return_settle(request, pk, return_pk):
    ret = get_object_or_404(company_queryset(SaleReturn.objects.all(), request), pk=return_pk, sale_id=pk, sale__company=request.company)
    try:
        settled = services.settle_return(
            request.user, ret,
            refund_method=request.POST.get("refund_method", ""),
            refund_cash_amount=dec(request.POST.get("refund_cash_amount"), None),
            shift=current_shift(request.user, request.company), company=request.company,
        )
        messages.success(
            request,
            f"Виплату за повернення {settled.number} зафіксовано: "
            f"готівка {settled.cash_amount} ₴, картка {settled.card_amount} ₴",
        )
    except StockError as e:
        messages.error(request, str(e))
    return redirect("sale_detail", pk=pk)


# ---------- Inventory ----------
@login_required
@company_required
@tenant_staff_only
def inventory(request):
    qs = company_queryset(Product.objects.select_related("category").filter(is_active=True), request)
    q, cat, st = request.GET.get("q", ""), request.GET.get("category", ""), request.GET.get("status", "")
    if q:
        qs = qs.filter(Q(name_lc__contains=q.lower()) | Q(sku__icontains=q) | Q(barcode__icontains=q))
    if cat.isdigit():
        qs = qs.filter(category_id=cat)
    if st == "out":
        qs = qs.filter(current_stock__lte=0)
    elif st == "low":
        qs = qs.filter(current_stock__gt=0, current_stock__lte=F("min_stock"))
    elif st == "ok":
        qs = qs.filter(current_stock__gt=F("min_stock"))
    return render(request, "core/inventory.html", {"products": qs, "categories": company_queryset(Category.objects.all(), request),
                                                   "q": q, "cat": cat, "st": st})


@login_required
@company_required
@tenant_staff_only
def inventory_adjust(request):
    form = AdjustForm(request.POST or None)
    form.fields["product"].queryset = company_queryset(Product.objects.filter(is_active=True), request)
    if request.method == "POST" and form.is_valid():
        try:
            services.apply_adjustment(request.user, form, company=request.company)
            messages.success(request, "Коригування проведено")
            return redirect("inventory_movements")
        except StockError as e:
            messages.error(request, str(e))
    return render(request, "core/form.html", {"form": form, "title": "Коригування запасів", "back": "inventory"})


@login_required
@company_required
@tenant_staff_only
def inventory_count_list(request):
    counts = InventoryCount.objects.filter(company=request.company).select_related("created_by", "approved_by")
    return render(request, "core/inventory_count_list.html", {"counts": counts[:200]})


@login_required
@company_required
@tenant_staff_only
@require_POST
def inventory_count_new(request):
    from django.db import transaction as db_transaction
    with db_transaction.atomic():
        count = InventoryCount.objects.create(
            company=request.company,
            created_by=request.user,
            note=(request.POST.get("note") or "").strip()[:2000],
        )
        products = Product.objects.select_for_update().filter(
            company=request.company, is_active=True, product_kind="goods", tracking_mode="none"
        ).order_by("pk")
        InventoryCountItem.objects.bulk_create([
            InventoryCountItem(
                company=request.company, inventory_count=count, product=product,
                expected_quantity=product.current_stock,
            ) for product in products.iterator()
        ], batch_size=500)
    messages.success(request, f"Створено {count.number}. До документа додано товари зі звичайним складським обліком.")
    return redirect("inventory_count_detail", pk=count.pk)


@login_required
@company_required
@tenant_staff_only
def inventory_count_detail(request, pk):
    count = get_object_or_404(
        InventoryCount.objects.select_related("created_by", "approved_by"),
        pk=pk, company=request.company,
    )
    if request.method == "POST":
        if count.status != InventoryCount.DRAFT:
            messages.error(request, "Затверджений документ доступний лише для перегляду.")
            return redirect("inventory_count_detail", pk=count.pk)
        if request.POST.get("action") == "approve":
            try:
                services.approve_inventory_count(request.user, count, request.company)
                messages.success(request, f"Інвентаризацію {count.number} затверджено. Різниці записані в рух товарів.")
                return redirect("inventory_count_detail", pk=count.pk)
            except StockError as exc:
                messages.error(request, str(exc))
        else:
            items = InventoryCountItem.objects.filter(inventory_count=count, company=request.company).select_related("product")
            updates = []
            for item in items:
                key = f"counted_{item.pk}"
                if key not in request.POST:
                    continue
                raw = (request.POST.get(key) or "").strip().replace(",", ".")
                if not raw:
                    item.counted_quantity = None
                    item.counted_at = None
                else:
                    value = dec(raw)
                    if value is None or value < 0 or value > Decimal("99999999999.999"):
                        messages.error(request, f"Некоректна кількість для товару «{item.product.name}».")
                        return redirect("inventory_count_detail", pk=count.pk)
                    item.counted_quantity = value
                    item.counted_at = timezone.now()
                updates.append(item)
            if updates:
                InventoryCountItem.objects.bulk_update(updates, ["counted_quantity", "counted_at"])
            barcode = (request.POST.get("barcode") or "").strip()
            if barcode:
                product = Product.objects.filter(company=request.company).filter(
                    Q(barcode=barcode) | Q(extra_barcodes__barcode=barcode)
                ).distinct().first()
                if not product:
                    messages.error(request, f"Штрих-код «{barcode}» не знайдено.")
                else:
                    item = InventoryCountItem.objects.filter(
                        inventory_count=count, company=request.company, product=product
                    ).first()
                    if not item:
                        messages.error(request, f"Товар «{product.name}» не входить до цього документа. Перевірте тип складського обліку.")
                    else:
                        item.counted_quantity = (item.counted_quantity or Decimal("0")) + Decimal("1")
                        item.counted_at = timezone.now()
                        item.save(update_fields=["counted_quantity", "counted_at"])
                        messages.success(request, f"{product.name}: зафіксовано {item.counted_quantity}.")
            else:
                messages.success(request, "Підраховані кількості збережено.")
        return redirect("inventory_count_detail", pk=count.pk)

    items = InventoryCountItem.objects.filter(
        inventory_count=count, company=request.company
    ).select_related("product")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all")
    if q:
        items = items.filter(
            Q(product__name__icontains=q) | Q(product__sku__icontains=q)
            | Q(product__barcode__icontains=q) | Q(product__extra_barcodes__barcode__icontains=q)
        ).distinct()
    all_items = InventoryCountItem.objects.filter(inventory_count=count, company=request.company)
    if status == "counted":
        items = items.filter(counted_quantity__isnull=False)
    elif status == "pending":
        items = items.filter(counted_quantity__isnull=True)
    elif status in ("difference", "surplus", "shortage"):
        items = items.filter(counted_quantity__isnull=False)
        baseline = "book_quantity_at_approval" if count.status == InventoryCount.APPROVED else "expected_quantity"
        if status == "surplus":
            items = items.filter(**{f"counted_quantity__gt": F(baseline)})
        elif status == "shortage":
            items = items.filter(**{f"counted_quantity__lt": F(baseline)})
        else:
            items = items.exclude(counted_quantity=F(baseline))
    summary = all_items.aggregate(
        total=Count("pk"), counted=Count("pk", filter=Q(counted_quantity__isnull=False)),
    )
    baseline_field = "book_quantity_at_approval" if count.status == InventoryCount.APPROVED else "expected_quantity"
    counted_rows = all_items.filter(counted_quantity__isnull=False).values("counted_quantity", baseline_field)
    surplus = shortage = D0
    for row in counted_rows:
        delta = row["counted_quantity"] - (row[baseline_field] or D0)
        if delta > 0:
            surplus += delta
        elif delta < 0:
            shortage += abs(delta)
    summary.update({"surplus": surplus, "shortage": shortage, "net_difference": surplus - shortage})
    return render(request, "core/inventory_count_detail.html", {
        "count": count, "items": items[:1000], "q": q, "status": status, "summary": summary,
    })


@login_required
@company_required
@tenant_staff_only
def inventory_count_print(request, pk):
    count = get_object_or_404(InventoryCount, pk=pk, company=request.company)
    items = InventoryCountItem.objects.filter(
        inventory_count=count, company=request.company
    ).select_related("product")
    return render(request, "core/inventory_count_print.html", {
        "count": count, "items": items, "printed_at": timezone.now(),
    })


@login_required
@company_required
@tenant_staff_only
def stock_batches(request):
    today = timezone.localdate()
    qs = StockBatch.objects.select_related("product", "purchase_item__purchase").filter(
        company=request.company, remaining_quantity__gt=0
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(product__sku__icontains=q) | Q(batch_number__icontains=q))
    status = request.GET.get("status", "all")
    if status == "expired":
        qs = qs.filter(expiry_date__lt=today)
    elif status == "soon":
        qs = qs.filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=30))
    elif status == "unknown":
        qs = qs.filter(expiry_date__isnull=True)
    batches = []
    for batch in qs.order_by(F("expiry_date").asc(nulls_last=True), "received_at", "pk")[:500]:
        days_left = (batch.expiry_date - today).days if batch.expiry_date else None
        batches.append({"batch": batch, "days_left": days_left,
                        "state": "expired" if days_left is not None and days_left < 0 else
                                 "soon" if days_left is not None and days_left <= 30 else
                                 "unknown" if days_left is None else "ok"})
    counts = {
        "expired": StockBatch.objects.filter(company=request.company, remaining_quantity__gt=0, expiry_date__lt=today).count(),
        "soon": StockBatch.objects.filter(company=request.company, remaining_quantity__gt=0,
            expiry_date__gte=today, expiry_date__lte=today + timedelta(days=30)).count(),
        "unknown": StockBatch.objects.filter(company=request.company, remaining_quantity__gt=0, expiry_date__isnull=True).count(),
    }
    return render(request, "core/stock_batches.html", {
        "batches": batches, "counts": counts, "status": status, "q": q,
    })


@login_required
@company_required
@tenant_staff_only
def stock_serials(request):
    qs = StockSerialUnit.objects.select_related("product", "purchase_item__purchase", "sale_item__sale").filter(
        company=request.company
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(serial_number__icontains=q) | Q(normalized_serial__icontains=q)
                       | Q(product__name__icontains=q) | Q(product__sku__icontains=q))
    status = request.GET.get("status", "all")
    if status in {StockSerialUnit.IN_STOCK, StockSerialUnit.SOLD}:
        qs = qs.filter(status=status)
    counts = {
        "in_stock": StockSerialUnit.objects.filter(company=request.company, status=StockSerialUnit.IN_STOCK).count(),
        "sold": StockSerialUnit.objects.filter(company=request.company, status=StockSerialUnit.SOLD).count(),
    }
    return render(request, "core/stock_serials.html", {
        "units": qs.order_by("-received_at", "pk")[:500], "counts": counts, "status": status, "q": q,
    })


@login_required
@company_required
@tenant_staff_only
def inventory_movements(request):
    qs = company_queryset(StockMovement.objects.select_related("product", "user"), request)
    q = request.GET.get("q", "").strip()
    product_id = request.GET.get("product", "").strip()
    movement_type = request.GET.get("type", "").strip()
    if product_id.isdigit():
        qs = qs.filter(product_id=int(product_id))
    valid_types = {value for value, _label in StockMovement.TYPES}
    if movement_type in valid_types:
        qs = qs.filter(movement_type=movement_type)
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(product__sku__icontains=q)
                       | Q(doc_ref__icontains=q) | Q(note__icontains=q) | Q(user__username__icontains=q))
    d1, d2 = get_date(request, "from", None), get_date(request, "to", None)
    if d1:
        qs = qs.filter(created_at__date__gte=d1)
    if d2:
        qs = qs.filter(created_at__date__lte=d2)
    if d1 and d2 and d1 > d2:
        qs = qs.none()
    count = qs.count()
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="stock-movements.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(["Дата", "Товар", "Артикул", "Тип", "Зміна", "Залишок", "Документ", "Оператор", "Примітка"])
        for move in qs.iterator():
            writer.writerow([
                timezone.localtime(move.created_at).strftime("%d.%m.%Y %H:%M"),
                move.product.name, move.product.sku, move.get_movement_type_display(),
                move.quantity_change, move.balance_after, move.doc_ref,
                move.user.username if move.user_id else "", move.note,
            ])
        return response
    totals = qs.aggregate(
        incoming=Sum("quantity_change", filter=Q(quantity_change__gt=0)),
        outgoing=Sum("quantity_change", filter=Q(quantity_change__lt=0)),
    )
    return render(request, "core/movements.html", {
        "moves": qs[:300], "movement_count": count, "products": company_queryset(Product.objects.all(), request),
        "types": StockMovement.TYPES, "q": q, "selected_product": product_id,
        "selected_type": movement_type, "date_from": request.GET.get("from", ""),
        "date_to": request.GET.get("to", ""),
        "incoming_total": totals["incoming"] or D0, "outgoing_total": totals["outgoing"] or D0,
        "net_total": (totals["incoming"] or D0) + (totals["outgoing"] or D0),
    })


# ---------- Customers ----------
@login_required
@company_required
@tenant_staff_only
def customer_list(request):
    qs = company_queryset(Customer.objects.annotate(last=Max("sales__created_at", filter=Q(sales__company=request.company, sales__status="done"))), request)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name_lc__contains=q.lower()) | Q(phone__icontains=q))
    return render(request, "core/customer_list.html", {"customers": qs, "q": q})


@login_required
@company_required
@tenant_staff_only
def customer_edit(request, pk=None):
    return simple_form(request, CustomerForm, "Клієнт", "customer_list", pk, Customer)


@login_required
@company_required
@tenant_staff_only
def customer_detail(request, pk):
    c = get_object_or_404(company_queryset(Customer.objects.all(), request), pk=pk)
    sales = c.sales.filter(company=request.company, status="done").prefetch_related("items__product")
    return render(request, "core/customer_detail.html", {"c": c, "sales": sales})


# ---------- Reports ----------
NET = ExpressionWrapper(F("quantity") - F("returned_qty"), output_field=DEC)


def _items(d1, d2, company):
    return SaleItem.objects.filter(company=company, sale__company=company, sale__status="done",
                                   sale__created_at__date__range=(d1, d2))


@login_required
@company_required
@tenant_staff_only
def report_daily(request):
    d = get_date(request, "date", timezone.localdate())
    sales = done_sales(request.company).filter(created_at__date=d)
    split = sales.aggregate(c=Sum("cash_amount"), k=Sum("card_amount"))
    total = sales.aggregate(s=Sum("total"))["s"] or D0
    return_qs = SaleReturn.objects.filter(company=request.company, created_at__date=d)
    return_split = return_qs.aggregate(c=Sum("cash_amount"), k=Sum("card_amount"))
    returns_total = return_qs.aggregate(s=Sum("total"))["s"] or D0
    returns_cash, returns_card = return_split["c"] or D0, return_split["k"] or D0
    pending_returns = return_qs.filter(refund_method="pending").aggregate(s=Sum("total"))["s"] or D0
    items = (_items(d, d, request.company).values("product__name", "product__sku")
             .annotate(q=Sum("quantity"), s=Sum(ExpressionWrapper(F("quantity") * F("price"), output_field=DEC)))
             .order_by("-s"))
    return render(request, "core/report_daily.html", {
        "d": d, "total": total, "returns_total": returns_total, "net_total": total - returns_total,
        "count": sales.count(),
        "units": _items(d, d, request.company).aggregate(s=Sum("quantity"))["s"] or D0,
        "cash": (split["c"] or D0) - returns_cash,
        "card": (split["k"] or D0) - returns_card,
        "returns_cash": returns_cash, "returns_card": returns_card,
        "pending_returns": pending_returns, "items": items})


@login_required
@company_required
@tenant_staff_only
def report_monthly(request):
    today = timezone.localdate()
    try:
        y, m = map(int, request.GET.get("month", "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    prev_first = prev_last.replace(day=1)

    def agg(f):
        qs = done_sales(request.company).filter(created_at__year=f.year, created_at__month=f.month)
        gross = qs.aggregate(s=Sum("total"))["s"] or D0
        return_qs = SaleReturn.objects.filter(company=request.company, created_at__year=f.year, created_at__month=f.month)
        returned = return_qs.aggregate(s=Sum("total"))["s"] or D0
        return qs, gross - returned, gross, returned
    qs, total, gross_total, returns_total = agg(first)
    _, prev_total, _, _ = agg(prev_first)
    month_returns = SaleReturn.objects.filter(company=request.company, created_at__year=first.year, created_at__month=first.month)
    return_split = month_returns.aggregate(c=Sum("cash_amount"), k=Sum("card_amount"))
    returns_cash, returns_card = return_split["c"] or D0, return_split["k"] or D0
    pending_returns = month_returns.filter(refund_method="pending").aggregate(s=Sum("total"))["s"] or D0
    sale_split = qs.aggregate(c=Sum("cash_amount"), k=Sum("card_amount"))
    cash_net = (sale_split["c"] or D0) - returns_cash
    card_net = (sale_split["k"] or D0) - returns_card
    days = qs.annotate(day=TruncDate("created_at")).values("day").annotate(s=Sum("total"), n=Count("id")).order_by("day")
    nd = max(len(days), 1)
    pct = ((total - prev_total) / prev_total * 100) if prev_total else None
    return render(request, "core/report_monthly.html", {
        "first": first, "total": total, "gross_total": gross_total, "returns_total": returns_total,
        "avg": total / nd, "count": qs.count(), "days": days,
        "prev_total": prev_total, "diff": total - prev_total, "pct": pct,
        "cash_net": cash_net, "card_net": card_net,
        "returns_cash": returns_cash, "returns_card": returns_card,
        "pending_returns": pending_returns})


@login_required
@company_required
@tenant_staff_only
def report_top(request):
    today = timezone.localdate()
    d1, d2 = get_date(request, "from", today.replace(day=1)), get_date(request, "to", today)
    n = int(request.GET.get("n", 10)) if request.GET.get("n", "10").isdigit() else 10
    base = (_items(d1, d2, request.company).values("product__name", "product__sku")
            .annotate(q=Sum(NET), s=Sum(ExpressionWrapper(NET * F("price"), output_field=DEC)), n=Count("sale", distinct=True)))
    return render(request, "core/report_top.html", {"d1": d1, "d2": d2, "n": n,
                                                    "by_sum": base.order_by("-s")[:n], "by_qty": base.order_by("-q")[:n]})


@login_required
@company_required
@tenant_staff_only
def report_profit(request):
    today = timezone.localdate()
    d1, d2 = get_date(request, "from", today.replace(day=1)), get_date(request, "to", today)
    # Margin is realized only after the item is handed over to the customer.
    # Supplier-order sales remain in revenue reports, but are not counted as earned
    # profit until fulfilment sets earned_at; returns reduce both net revenue and cost.
    base = SaleItem.objects.filter(
        company=request.company, sale__company=request.company, sale__status=Sale.DONE,
        earned_at__date__range=(d1, d2),
    )
    rows = list(base.values("product__name", "product__sku", "product__category__name").annotate(
        q=Sum(NET), rev=Sum(ExpressionWrapper(NET * F("price"), output_field=DEC)),
        cost=Sum(ExpressionWrapper(NET * F("cost_price"), output_field=DEC)),
    ).order_by("-rev"))
    for r in rows:
        r["margin"] = (r["rev"] or D0) - (r["cost"] or D0)
        r["pct"] = r["margin"] / r["rev"] * 100 if r["rev"] else D0
    category_rows = list(base.values("product__category__name").annotate(
        q=Sum(NET), rev=Sum(ExpressionWrapper(NET * F("price"), output_field=DEC)),
        cost=Sum(ExpressionWrapper(NET * F("cost_price"), output_field=DEC)),
    ).order_by("-rev"))
    for r in category_rows:
        r["margin"] = (r["rev"] or D0) - (r["cost"] or D0)
        r["pct"] = r["margin"] / r["rev"] * 100 if r["rev"] else D0
    rev = sum((r["rev"] or D0 for r in rows), D0)
    cost = sum((r["cost"] or D0 for r in rows), D0)
    low_margin_rows = sorted(rows, key=lambda r: (r["pct"], r["margin"]))[:10]
    return render(request, "core/report_profit.html", {
        "d1": d1, "d2": d2, "rows": rows, "category_rows": category_rows,
        "low_margin_rows": low_margin_rows, "rev": rev, "cost": cost, "margin": rev - cost,
    })

@login_required
@company_required
@tenant_staff_only
def report_stock(request):
    today = timezone.localdate()
    d1, d2 = get_date(request, "from", today - timedelta(days=89)), get_date(request, "to", today)
    if d1 > d2:
        d1, d2 = d2, d1
    days = max((d2 - d1).days + 1, 1)
    products = company_queryset(Product.objects.filter(is_active=True), request).select_related("category")
    stock = products.aggregate(
        value=Sum(ExpressionWrapper(F("current_stock") * F("cost_price"), output_field=DEC)),
        quantity=Sum("current_stock"),
        product_count=Count("pk", filter=Q(current_stock__gt=0)),
    )
    value = stock["value"] or D0
    summary = company_queryset(StockMovement.objects.all(), request).values("movement_type").annotate(s=Sum("quantity_change"), n=Count("id"))
    names = dict(StockMovement.TYPES)
    for r in summary:
        r["label"] = names.get(r["movement_type"], r["movement_type"])

    # Sales velocity and ABC are calculated from completed sales in the selected period.
    # Supplier-order items are excluded from stock demand; returns reduce net sold quantity.
    sales_rows = list(_items(d1, d2, request.company).filter(
        source=SaleItem.STOCK
    ).values("product_id", "product__name", "product__sku", "product__category__name").annotate(
        qty=Sum(NET),
        revenue=Sum(ExpressionWrapper(NET * F("price"), output_field=DEC)),
        margin=Sum(ExpressionWrapper(NET * (F("price") - F("cost_price")), output_field=DEC)),
    ).order_by("-revenue"))
    total_revenue = sum((r["revenue"] or D0 for r in sales_rows), D0)
    product_map = {p.pk: p for p in products}
    cumulative = D0
    abc_rows = []
    sales_by_product = {}
    for row in sales_rows:
        revenue = row["revenue"] or D0
        cumulative += revenue
        share = (revenue / total_revenue * 100) if total_revenue > 0 else D0
        cumulative_share = (cumulative / total_revenue * 100) if total_revenue > 0 else D0
        row["share"] = share
        row["cumulative_share"] = cumulative_share
        row["abc"] = "A" if cumulative_share <= 80 or (cumulative_share - share) < 80 else ("B" if cumulative_share <= 95 or (cumulative_share - share) < 95 else "C")
        row["daily_qty"] = (row["qty"] or D0) / Decimal(days)
        product = product_map.get(row["product_id"])
        row["stock"] = product.current_stock if product else D0
        row["min_stock"] = product.min_stock if product else D0
        row["stock_value"] = (product.current_stock * product.cost_price) if product else D0
        row["coverage_days"] = (product.current_stock / row["daily_qty"]) if product and row["daily_qty"] > 0 else None
        row["replenishment"] = bool(product and product.current_stock <= product.min_stock)
        sales_by_product[row["product_id"]] = row
        abc_rows.append(row)

    for product in products.filter(current_stock__gt=0).exclude(pk__in=sales_by_product.keys()).order_by("name"):
        abc_rows.append({
            "product_id": product.pk, "product__name": product.name, "product__sku": product.sku,
            "product__category__name": product.category.name if product.category else "",
            "qty": D0, "revenue": D0, "margin": D0, "share": D0,
            "cumulative_share": D0, "abc": "—", "daily_qty": D0,
            "stock": product.current_stock, "min_stock": product.min_stock,
            "stock_value": product.current_stock * product.cost_price,
            "coverage_days": None, "replenishment": product.current_stock <= product.min_stock,
        })

    abc_summary = {}
    for code in ("A", "B", "C", "—"):
        rows = [r for r in abc_rows if r["abc"] == code]
        abc_summary[code] = {
            "count": len(rows),
            "revenue": sum((r["revenue"] or D0 for r in rows), D0),
            "stock_value": sum((r["stock_value"] or D0 for r in rows), D0),
        }
    stagnant = []
    sold = SaleItem.objects.filter(
        company=request.company, sale__company=request.company, sale__status=Sale.DONE,
        source=SaleItem.STOCK,
    ).values("product_id").annotate(last_sold=Max("sale__created_at"))
    sold_by_product = {row["product_id"]: row["last_sold"] for row in sold}
    for product in products.filter(current_stock__gt=0).order_by("-current_stock"):
        last_sold = sold_by_product.get(product.pk)
        age = (today - timezone.localtime(last_sold).date()).days if last_sold else None
        if age is None or age >= 30:
            stagnant.append({
                "product": product, "last_sold": last_sold, "days": age,
                "stock_value": product.current_stock * product.cost_price,
                "status": "never" if age is None else ("90+" if age >= 90 else ("60+" if age >= 60 else "30+")),
            })
    stagnant.sort(key=lambda row: (row["days"] is not None, -(row["days"] or 999999), -row["stock_value"]))
    low = products.filter(current_stock__lte=F("min_stock")).order_by("current_stock", "name")
    replenishment_rows = []
    for product in low:
        sold_row = sales_by_product.get(product.pk, {})
        replenishment_rows.append({
            "product__name": product.name, "product__sku": product.sku,
            "stock": product.current_stock, "min_stock": product.min_stock,
            "daily_qty": sold_row.get("daily_qty", D0),
        })
    return render(request, "core/report_stock.html", {
        "value": value, "quantity": stock["quantity"] or D0, "product_count": stock["product_count"] or 0,
        "summary": summary, "low": low, "stagnant": stagnant[:300],
        "stagnant_value": sum((r["stock_value"] for r in stagnant), D0),
        "stagnant_qty": sum((r["product"].current_stock for r in stagnant), D0),
        "stagnant_count": len(stagnant), "today": today,
        "d1": d1, "d2": d2, "days": days, "abc_rows": abc_rows[:500],
        "abc_summary": abc_summary, "no_sales_summary": abc_summary["—"], "total_revenue": total_revenue,
        "replenishment_rows": replenishment_rows[:100],
    })

# ---------- Shifts ----------
def shift_summary(shift):
    qs = done_sales(shift.company).filter(shift=shift)
    a = qs.aggregate(t=Sum("total"), c=Sum("cash_amount"), k=Sum("card_amount"), ch=Sum("change_given"))
    settlement_ops = shift.cash_ops.filter(
        company=shift.company, detail__source__in=["settlement", "settlement_reversal"]
    )
    settlement_net = settlement_ops.aggregate(total=Sum("amount"))["total"] or D0
    advance_ops = shift.cash_ops.filter(
        company=shift.company, detail__source__in=["advance", "advance_reversal"]
    )
    advance_net = advance_ops.aggregate(total=Sum("amount"))["total"] or D0
    return {"total": a["t"] or D0, "count": qs.count(), "cash": a["c"] or D0, "card": a["k"] or D0,
            "settlement_net": settlement_net, "advance_net": advance_net,
            "expected": cashlib.to_dec(cashlib.total(shift.drawer))}


def _counts_from_post(request, prefix, fallback=None):
    found, out = False, {}
    for d in cashlib.DENOMS:
        raw = request.POST.get(f"{prefix}{d}")
        if raw is not None:
            found = True
            if raw.strip().isdigit():
                out[d] = int(raw)
    return cashlib.clean(out) if found else fallback


def _drawer_rows(counts):
    counts = cashlib.clean(counts)
    return [{"k": d, "l": cashlib.label(d), "n": counts.get(d, 0)} for d in cashlib.DENOMS]


@tenant_cashier_only
@transaction.atomic
def shift_view(request):
    shift = current_shift(request.user, request.company)
    if request.method == "POST":
        # Serialize open/close actions for this cashier and refresh the shift under lock.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        shift = Shift.objects.select_for_update().filter(
            company=request.company, cashier=request.user, closed_at__isnull=True
        ).first()
        action = request.POST.get("action")
        if action == "open" and not shift:
            counts = _counts_from_post(request, "c_", {})
            cash_total = cashlib.to_dec(cashlib.total(counts))
            if not counts and dec(request.POST.get("cash")):
                cash_total = dec(request.POST.get("cash"), D0)
            Shift.objects.create(company=request.company, cashier=request.user, opening_cash=cash_total,
                                 opening_counts=cashlib.store(counts), drawer=cashlib.store(counts))
            return redirect("pos")
        if action == "close" and shift:
            expected = cashlib.clean(shift.drawer)
            counted = _counts_from_post(request, "c_", expected)
            shift.closing_counted = cashlib.store(counted)
            shift.discrepancy = cashlib.to_dec(cashlib.total(counted) - cashlib.total(expected))
            shift.closed_at = timezone.now()
            shift.save()
            summ = shift_summary(shift)
            summ["counted"] = cashlib.to_dec(cashlib.total(counted))
            return render(request, "core/shift.html", {"closed": shift, "summary": summ})
    last = Shift.objects.filter(company=request.company, cashier=request.user, closed_at__isnull=False).first()
    prefill = (last.closing_counted if last and last.closing_counted is not None else (last.drawer if last else {}))
    ctx = {"shift": shift, "summary": shift_summary(shift) if shift else None,
           "history": Shift.objects.filter(company=request.company, cashier=request.user)[:10]}
    ctx["rows"] = _drawer_rows(shift.drawer if shift else prefill)
    return render(request, "core/shift.html", ctx)


@tenant_cashier_only
@transaction.atomic
def drawer_view(request):
    shift = current_shift(request.user, request.company)
    if request.method == "POST":
        # A checkout and a drawer adjustment must never overwrite each other's counts.
        shift = Shift.objects.select_for_update().filter(
            company=request.company, cashier=request.user, closed_at__isnull=True
        ).first()
    if not shift:
        messages.info(request, "Спочатку відкрийте зміну")
        return redirect("shift")
    if request.method == "POST":
        new = _counts_from_post(request, "c_", None)
        old = cashlib.clean(shift.drawer)
        if new is not None and new != old:
            delta_kop = cashlib.total(new) - cashlib.total(old)
            kind = request.POST.get("kind", "adjust")
            if kind not in ("in", "out", "adjust"):
                kind = "adjust"
            if (kind == "in" and delta_kop < 0) or (kind == "out" and delta_kop > 0):
                messages.error(request, "Тип касової операції не відповідає зміні суми. Перевірте купюри та повторіть.")
                return redirect("drawer")
            CashOperation.objects.create(
                company=request.company, shift=shift, user=request.user, kind=kind,
                amount=cashlib.to_dec(delta_kop), note=request.POST.get("note", "")[:200],
                detail={str(k): new.get(k, 0) - old.get(k, 0) for k in cashlib.DENOMS if new.get(k, 0) != old.get(k, 0)})
            shift.drawer = cashlib.store(new)
            shift.save(update_fields=["drawer"])
            messages.success(request, "Касу оновлено")
        return redirect("drawer")
    return render(request, "core/drawer.html", {"shift": shift, "rows": _drawer_rows(shift.drawer),
                                                "total": cashlib.to_dec(cashlib.total(shift.drawer)),
                                                "ops": shift.cash_ops.filter(company=request.company)[:20]})

# ---------- Settings ----------
@login_required
@company_required
@tenant_staff_only
def settings_home(request):
    return redirect("designer_home")


@login_required
@company_required
@tenant_staff_only
def business_profile(request):
    company = request.company
    form = BusinessProfileForm(request.POST or None, instance=company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Профіль магазину оновлено. Товари, залишки та історія не змінювалися.")
        return redirect("business_profile")
    return render(request, "core/business_profile.html", {"form": form, "company": company})


@login_required
@company_required
@tenant_staff_only
def company_settings(request):
    from django.forms import modelformset_factory
    AccFS = modelformset_factory(BankAccount, fields=["bank_name", "iban", "mfo", "holder", "currency", "note", "is_default"],
                                 extra=1, can_delete=True)
    company = request.company
    obj = CompanySettings.get(company)
    account_qs = BankAccount.objects.filter(company=company)
    form = CompanyForm(request.POST or None, request.FILES or None, instance=obj)
    fs = AccFS(request.POST or None, queryset=account_qs)
    if request.method == "POST" and form.is_valid() and fs.is_valid():
        form.save()
        for inst in fs.save(commit=False):
            if company:
                inst.company = company
            inst.save()
        for inst in fs.deleted_objects:
            inst.delete()
        messages.success(request, "Налаштування збережено")
        return redirect("company_settings")
    return render(request, "core/company_settings.html", {"form": form, "fs": fs})
