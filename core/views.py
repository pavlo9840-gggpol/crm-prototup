import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import (Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum)
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import cash as cashlib, pricing, services
from .forms import AdjustForm, CategoryForm, CompanyForm, CustomerForm, ProductForm, SupplierForm
from .models import (BankAccount, CashOperation, CompanySettings, Category, Customer, CustomerCar, MarkupTier, PriceHistory, Product,
                     Purchase, Sale, SaleItem, Shift, StockMovement, Supplier, SupplierItem)
from .models import norm_plate, norm_sku
from .services import StockError

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


def done_sales():
    return Sale.objects.filter(status=Sale.DONE)


def current_shift(user):
    return Shift.objects.filter(cashier=user, closed_at__isnull=True).first()


# ---------- Dashboard ----------
@login_required
def dashboard(request):
    if not request.user.is_staff:
        return redirect("pos")
    today = timezone.localdate()
    today_sales = done_sales().filter(created_at__date=today)
    month_sales = done_sales().filter(created_at__year=today.year, created_at__month=today.month)
    week = {r["day"]: r["s"] for r in done_sales().filter(created_at__date__gte=today - timedelta(days=6))
            .annotate(day=TruncDate("created_at")).values("day").annotate(s=Sum("total"))}
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    mx = max([week.get(d, D0) for d in days] + [Decimal("1")])
    bars = [{"day": d, "sum": week.get(d, D0), "h": int(week.get(d, D0) / mx * 100)} for d in days]
    ctx = {
        "today_total": today_sales.aggregate(s=Sum("total"))["s"] or D0,
        "today_count": today_sales.count(),
        "month_total": month_sales.aggregate(s=Sum("total"))["s"] or D0,
        "low_count": Product.objects.filter(is_active=True, current_stock__lte=F("min_stock")).count(),
        "last_sales": done_sales().select_related("cashier")[:10],
        "last_moves": StockMovement.objects.select_related("product")[:10],
        "bars": bars,
        "waiting_orders": SaleItem.objects.filter(source=SaleItem.SUPPLIER, sale__status=Sale.DONE,
                                                  fulfil_status__in=[SaleItem.ORDERED, SaleItem.ARRIVED]).count(),
        "overdue_orders": SaleItem.objects.filter(source=SaleItem.SUPPLIER, sale__status=Sale.DONE,
                                                  fulfil_status=SaleItem.ARRIVED, pickup_deadline__lt=today).count(),
    }
    return render(request, "core/dashboard.html", ctx)


# ---------- Products / categories / suppliers ----------
@staff_only
def product_list(request):
    qs = Product.objects.select_related("category")
    q, cat = request.GET.get("q", "").strip(), request.GET.get("category", "")
    if q:
        qn = norm_sku(q)
        cond = Q(name_lc__contains=q.lower()) | Q(sku__icontains=q) | Q(barcode__icontains=q) | Q(brand__icontains=q)
        if qn:
            cond |= Q(sku_norm__contains=qn)
        qs = qs.filter(cond)
    if cat:
        ids = [int(cat)] if cat.isdigit() else []
        ids += list(Category.objects.filter(parent_id__in=ids).values_list("id", flat=True))
        qs = qs.filter(category_id__in=ids)
    sort = request.GET.get("sort", "name")
    if sort.lstrip("-") in ("name", "current_stock", "price", "cost_price", "sku", "brand"):
        qs = qs.order_by(sort)
    from .designer_views import print_bar
    return render(request, "core/product_list.html",
                  {"products": qs, "categories": Category.objects.all(), "q": q, "cat": cat, **print_bar("pricetag")})


def all_brands():
    """Бренди зі складу та з прайсів постачальників (для підказок у формі)."""
    names = set(Product.objects.exclude(brand="").values_list("brand", flat=True).distinct())
    names |= set(SupplierItem.objects.exclude(brand="").values_list("brand", flat=True).distinct()[:3000])
    return sorted(names)[:3000]


PREFILL = ("brand", "sku", "name", "category", "unit", "cost_price", "price")


@staff_only
def product_edit(request, pk=None):
    obj = get_object_or_404(Product, pk=pk) if pk else None
    old = Product.objects.get(pk=pk) if pk else None
    initial = {k: request.GET[k] for k in PREFILL if request.GET.get(k)} if (not obj and request.method == "GET") else {}
    form = ProductForm(request.POST or None, request.FILES or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        p = form.save()
        if old:
            for f in ("price", "cost_price"):
                if getattr(old, f) != getattr(p, f):
                    PriceHistory.objects.create(product=p, field=f, old_value=getattr(old, f),
                                                new_value=getattr(p, f), changed_by=request.user)
        else:
            init = form.cleaned_data.get("initial_stock")
            if init and init > 0:
                services.add_movement(p, init, "opening", "", request.user, "Початковий залишок")
        if p.auto_price and pricing.reprice_product(p, request.user):
            messages.info(request, "Автоціна: ціну продажу перераховано за правилами цін")
        messages.success(request, f"Товар «{p.brand} {p.sku}» збережено")
        if request.POST.get("again") and not old:          # швидке додавання серії: бренд і категорія лишаються
            q = {"brand": p.brand, "unit": p.unit}
            if p.category_id:
                q["category"] = p.category_id
            return redirect(reverse("product_new") + "?" + urlencode(q))
        return redirect("product_list")
    history = obj.price_history.select_related("changed_by")[:20] if obj else []
    tiers = [{"id": t.pk, "name": t.name, "pct": float(t.percent)} for t in MarkupTier.objects.all()]
    dt = MarkupTier.objects.filter(is_default=True).first()
    return render(request, "core/product_form.html", {
        "form": form, "obj": obj, "history": history, "brands": all_brands(), "tiers": tiers,
        "default_pct": float(dt.percent) if dt else 30.0})


@staff_only
def product_lookup(request):
    """Підказки під час введення бренду й артикула: дубль на складі, пропозиції постачальників, дані про деталь."""
    from . import crosses
    from .models import PartInfo, PartOe
    brand = " ".join(request.GET.get("b", "").split()).upper()
    sn = norm_sku(request.GET.get("s", ""))
    if len(sn) < 2:
        return JsonResponse({"empty": True})
    exclude = request.GET.get("pk") or 0
    same = Product.objects.filter(sku_norm=sn).exclude(pk=exclude)
    dup = same.filter(brand__iexact=brand).first() if brand else same.filter(brand="").first()
    others = [{"brand": p.brand or "—", "name": p.name, "url": reverse("product_edit", args=[p.pk])}
              for p in same.exclude(pk=dup.pk if dup else 0)[:5]]
    pct = services.tier_pct(None)
    offers_qs = SupplierItem.objects.filter(sku_norm=sn).select_related("supplier").order_by("-available", "cost")
    if brand:
        offers_qs = offers_qs.filter(brand=brand)
    offers = [{"id": o.pk, "brand": o.brand, "sku": o.sku, "name": o.name, "supplier": o.supplier.name, "cost": float(o.cost),
               "qty": float(o.qty_avail) if o.qty_avail is not None else None, "available": o.available,
               "price": float(services.markup_price(o.cost, pct))} for o in offers_qs[:8]]
    ib = brand or (offers[0]["brand"] if offers else "")
    info = PartInfo.objects.filter(brand=ib, sku_norm=sn).first()
    card = reverse("part_card") + "?" + urlencode({"b": ib, "s": sn})
    data = {"dup": {"name": dup.name, "brand": dup.brand, "sku": dup.sku, "stock": float(dup.current_stock),
                    "url": reverse("product_edit", args=[dup.pk])} if dup else None,
            "others": others, "offers": offers, "card": card}
    if info or PartOe.objects.filter(brand=ib, sku_norm=sn).exists() or offers:
        data["info"] = {"description": (info.description[:260] if info else ""), "image": info.image_url if info else "",
                        "oe": PartOe.objects.filter(brand=ib, sku_norm=sn).count(),
                        "analogs": len(crosses.analogs(ib, sn, limit=50))}
    return JsonResponse(data)


@staff_only
def simple_form(request, model_form, title, back, pk=None, model=None):
    obj = get_object_or_404(model, pk=pk) if pk else None
    form = model_form(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Збережено")
        return redirect(back)
    return render(request, "core/form.html", {"form": form, "title": title, "back": back})


def category_list(request):
    return render(request, "core/simple_list.html", {"title": "Категорії", "items": Category.objects.all(),
                                                    "add": "category_new", "edit": "category_edit"})


def supplier_list(request):
    return render(request, "core/simple_list.html", {"title": "Постачальники", "items": Supplier.objects.all(),
                                                    "add": "supplier_new", "edit": "supplier_edit"})


category_list = staff_only(category_list)
supplier_list = staff_only(supplier_list)


def category_edit(request, pk=None):
    return simple_form(request, CategoryForm, "Категорія", "category_list", pk, Category)


def supplier_edit(request, pk=None):
    return simple_form(request, SupplierForm, "Постачальник", "supplier_list", pk, Supplier)


# ---------- Purchases ----------
@staff_only
def purchase_list(request):
    qs = Purchase.objects.select_related("supplier")
    d1, d2 = get_date(request, "from", None), get_date(request, "to", None)
    if d1:
        qs = qs.filter(created_at__date__gte=d1)
    if d2:
        qs = qs.filter(created_at__date__lte=d2)
    if request.GET.get("supplier"):
        qs = qs.filter(supplier_id=request.GET["supplier"])
    from .designer_views import print_bar
    return render(request, "core/purchase_list.html", {"purchases": qs[:300], "suppliers": Supplier.objects.all(), **print_bar("purchase")})


@staff_only
def purchase_new(request):
    if request.method == "POST":
        sid, new_name = request.POST.get("supplier"), request.POST.get("new_supplier", "").strip()
        lines = []
        product_ids = request.POST.getlist("product")
        quantities = request.POST.getlist("qty")
        costs = request.POST.getlist("cost")
        try:
            if not (len(product_ids) == len(quantities) == len(costs)):
                raise StockError("Дані рядків закупівлі неповні. Оновіть сторінку й повторіть.")
            for index, (pid, raw_q, raw_cost) in enumerate(zip(product_ids, quantities, costs), start=1):
                # Empty extra rows are normal in the form; partially filled rows are not.
                if not any(str(value or "").strip() for value in (pid, raw_q, raw_cost)):
                    continue
                p = Product.objects.filter(pk=pid or 0).first()
                q, cost = dec(raw_q), dec(raw_cost)
                if p is None:
                    raise StockError(f"Рядок {index}: оберіть товар зі списку.")
                if q is None or q <= 0:
                    raise StockError(f"Рядок {index}: кількість має бути більшою за нуль.")
                if cost is None or cost < 0:
                    raise StockError(f"Рядок {index}: закупівельна ціна не може бути від'ємною.")
                lines.append((p, q, cost))
            if not lines:
                raise StockError("Додайте хоча б один товар")
            if new_name and sid:
                raise StockError("Оберіть наявного постачальника або введіть назву нового, не обидва варіанти.")
            with transaction.atomic():
                supplier = Supplier.objects.create(name=new_name) if new_name else Supplier.objects.filter(pk=sid or 0, is_active=True).first()
                if not supplier:
                    raise StockError("Оберіть активного постачальника або введіть назву нового.")
                pur = services.create_purchase(request.user, supplier, lines, bool(request.POST.get("update_cost")))
            messages.success(request, f"Накладну {pur.number} проведено")
            return redirect("purchase_detail", pk=pur.pk)
        except StockError as e:
            messages.error(request, str(e))
    return render(request, "core/purchase_form.html", {
        "suppliers": Supplier.objects.filter(is_active=True),
        "products": Product.objects.filter(is_active=True)})


@staff_only
def purchase_detail(request, pk):
    return render(request, "core/purchase_detail.html",
                  {"p": get_object_or_404(Purchase.objects.select_related("supplier"), pk=pk)})


# ---------- POS ----------
@login_required
def pos(request):
    shift = current_shift(request.user)
    if not shift and not request.user.is_staff:
        messages.info(request, "Спочатку відкрийте зміну")
        return redirect("shift")
    init = {"cart": [], "customer": None, "resume_id": None}
    rid = request.GET.get("resume")
    if rid:
        held = get_object_or_404(Sale, pk=rid, status=Sale.HELD)
        if held.cashier_id != request.user.id and not request.user.is_staff:
            raise PermissionDenied
        init["resume_id"] = held.pk
        init["payment"] = held.payment_method
        if held.customer:
            init["customer"] = _customer_json(held.customer)
            init["car"] = _car_json(held.car) if held.car_id else None
        init["cart"] = [_cart_line_json(i) for i in held.items.select_related("product", "supplier_item__supplier")]
    cats = {c.pk: c for c in Category.objects.all()}

    def root(cid):
        while cid and cats[cid].parent_id:
            cid = cats[cid].parent_id
        return cid
    products = [{"id": p.pk, "n": p.name, "s": p.sku, "b": p.barcode or "", "p": float(p.price), "a": p.allow_price_change,
                 "st": float(p.current_stock), "c": root(p.category_id), "f": p.is_favorite, "u": p.unit,
                 "k": float(p.cost_price), "br": p.brand}
                for p in Product.objects.filter(is_active=True).exclude(from_supplier=True, current_stock__lte=0)]
    init["products"] = products
    init["tiers"] = [{"id": t.pk, "name": t.name, "pct": float(t.percent)} for t in MarkupTier.objects.all()]
    dt = MarkupTier.objects.filter(is_default=True).first()
    init["default_pct"] = float(dt.percent) if dt else 0
    init["cats"] = [{"id": c.pk, "name": c.name} for c in cats.values() if not c.parent_id]
    init["denoms"] = [{"k": d, "l": cashlib.label(d)} for d in cashlib.DENOMS]
    init["drawer"] = float(cashlib.to_dec(cashlib.total(shift.drawer))) if shift else None
    cs = CompanySettings.get()
    init["auto_print"], init["auto_print_format"] = cs.auto_print, cs.auto_print_format
    return render(request, "core/pos.html", {"init": init, "shift": shift})


def _car_json(car):
    return {"id": car.pk, "title": car.title, "plate": car.plate, "vin": car.vin, "make": car.make, "model": car.model,
            "year": car.year}


def _customer_json(c):
    return {"id": c.pk, "name": c.name, "pct": float(services.tier_pct(c)), "tier": c.markup_tier_id,
            "cars": [_car_json(x) for x in c.cars.all()[:20]]}


def _cart_line_json(i):
    base = {"id": i.product_id, "name": i.product.name, "price": float(i.price), "orig": float(i.original_price),
            "qty": float(i.quantity), "unit": i.product.unit, "cost": float(i.cost_price)}
    si = i.supplier_item if i.source == SaleItem.SUPPLIER else None
    if si:
        return dict(base, key=f"s{si.pk}", si=si.pk, allow=True, name=f"{si.brand} {si.sku} {si.name}".strip(),
                    rl=pricing.rule_json(pricing.RuleSet().for_(si.brand, i.product.category_id)),
                    sup=si.supplier.name, avail=float(si.qty_avail) if si.qty_avail is not None else None)
    return dict(base, key=f"p{i.product_id}", allow=i.product.allow_price_change,
                name=f"{i.product.brand} {i.product.name}".strip())


def product_json(p):
    return {"id": p.pk, "name": p.name, "sku": p.sku, "barcode": p.barcode or "", "price": float(p.price),
            "allow": p.allow_price_change, "stock": float(p.current_stock), "unit": p.unit}


@login_required
def pos_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"results": [], "exact": False})
    qs = Product.objects.filter(is_active=True)
    exact = qs.filter(Q(barcode=q) | Q(sku__iexact=q)).first()
    if exact:
        return JsonResponse({"results": [product_json(exact)], "exact": True})
    res = qs.filter(Q(name_lc__contains=q.lower()) | Q(sku__icontains=q) | Q(barcode__icontains=q))[:15]
    return JsonResponse({"results": [product_json(p) for p in res], "exact": False})


@login_required
def pos_customer(request):
    q = request.GET.get("q", "").strip()
    cond = Q(name_lc__contains=q.lower()) | Q(phone__icontains=q)
    pn = norm_plate(q)
    if len(pn) >= 3:                                   # пошук за держномером або VIN авто
        cond |= Q(cars__plate_norm__contains=pn)
    if len(q) >= 5:
        cond |= Q(cars__vin__icontains=q)
    res = Customer.objects.filter(cond).distinct().prefetch_related("cars")[:8] if q else []
    return JsonResponse({"results": [dict(_customer_json(c), phone=c.phone) for c in res]})


@login_required
@require_POST
def pos_customer_new(request):
    d = _jb(request)
    name, phone = d.get("name", "").strip(), d.get("phone", "").strip()
    if not name or not phone:
        return JsonResponse({"error": "Потрібні ім'я і телефон"}, status=400)
    c, _ = Customer.objects.get_or_create(phone=phone, defaults={"name": name})
    return JsonResponse(_customer_json(c))


def _parse_cart(request):
    d = _jb(request)
    lines = []
    customer = Customer.objects.filter(pk=d.get("customer_id") or 0).select_related("markup_tier").first()
    for it in d.get("items", []):
        q = dec(it.get("qty"))
        if it.get("si"):                       # позиція з прайсу постачальника: ціна = закупівельна + націнка
            si = SupplierItem.objects.select_related("supplier").filter(pk=it["si"], available=True).first()
            if not si or not q or q <= 0:
                raise StockError("Позиції немає в актуальному прайсі постачальника")
            p = services.ensure_product(si)
            listp = pricing.list_price(si.cost, si.brand, p.category_id, customer)[0]
            price = dec(it.get("price"), listp)
            if price is None or price < 0:
                price = listp
            lines.append((p, q, price, {"si": si, "list": listp}))
            continue
        p = Product.objects.filter(pk=it.get("id"), is_active=True).first()
        if not p or not q or q <= 0:
            raise StockError("Некоректний рядок чека")
        price = p.price
        if p.allow_price_change:
            price = dec(it.get("price"), p.price)
            if price < 0:
                price = p.price
        lines.append((p, q, price, None))
    pay = d.get("payment") if d.get("payment") in ("cash", "card", "mixed") else "cash"
    held = None
    if d.get("resume_id"):
        held = Sale.objects.filter(pk=d["resume_id"], status=Sale.HELD).first()
        if held is None:
            raise StockError("Чернетка вже проведена або видалена. Оновіть касу та створіть чек заново.")
        if held.cashier_id != request.user.id and not request.user.is_staff:
            raise PermissionDenied
    car = None
    if customer and d.get("car_id"):
        car = CustomerCar.objects.filter(pk=d["car_id"], customer=customer).first()
    return lines, pay, customer, held, car


def _payment_inputs(d):
    def kop(x):
        v = dec(x)
        return cashlib.to_kop(v) if v is not None else None
    return d.get("payment") if d.get("payment") in ("cash", "card", "mixed") else "cash", \
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


@login_required
@require_POST
def pos_car_new(request):
    d = _jb(request)
    cust = get_object_or_404(Customer, pk=d.get("customer_id") or 0)
    f, err = _clean_car(d)
    if err:
        return JsonResponse({"error": err}, status=400)
    car = CustomerCar.objects.create(customer=cust, **f)
    return JsonResponse(_car_json(car))


@login_required
@require_POST
def car_add(request, pk):
    cust = get_object_or_404(Customer, pk=pk)
    f, err = _clean_car(request.POST)
    if err:
        messages.error(request, err)
    else:
        CustomerCar.objects.create(customer=cust, **f)
        messages.success(request, "Авто додано")
    return redirect("customer_detail", pk=pk) if request.user.is_staff else redirect("pos")


@staff_only
@require_POST
def car_delete(request, pk):
    car = get_object_or_404(CustomerCar, pk=pk)
    cid = car.customer_id
    car.delete()
    messages.success(request, "Авто видалено")
    return redirect("customer_detail", pk=cid)


@login_required
@require_POST
def pos_change(request):
    d = _jb(request)
    shift = current_shift(request.user)
    pay, card, received, counts = _payment_inputs(d)
    total_kop = cashlib.to_kop(dec(d.get("total"), D0))
    res = cashlib.compute_payment(total_kop, pay, card, received, counts, cashlib.clean(shift.drawer) if shift else None)
    return JsonResponse(_plan_json(res))


@login_required
@require_POST
@transaction.atomic
def pos_checkout(request):
    # Serialize checkouts in the same cash shift before reading/updating its drawer.
    shift = Shift.objects.select_for_update().filter(cashier=request.user, closed_at__isnull=True).first()
    if not shift and not request.user.is_staff:
        return JsonResponse({"error": "Зміну не відкрито"}, status=400)
    try:
        lines, pay, customer, held, car = _parse_cart(request)
        merged = services.merge_lines(lines)
        total_kop = sum(cashlib.to_kop(q * pr) for _, q, pr, _x in merged)
        pm, card, received, counts = _payment_inputs(_jb(request))
        res = cashlib.compute_payment(total_kop, pm, card, received, counts,
                                      cashlib.clean(shift.drawer) if shift else None)
        if not res["ok"]:
            raise StockError(res["msg"])
        info = {"card": cashlib.to_dec(res["card"]), "received": cashlib.to_dec(res["received"]),
                "change": cashlib.to_dec(res["change"]), "drawer": res["drawer_after"],
                "detail": {"plan": {cashlib.label(k): n for k, n in res["plan"].items()},
                           "rounding": float(cashlib.to_dec(res["rounding"]))}}
        sale = services.finalize_sale(request.user, lines, pm, customer, shift, held, info, car=car)
    except StockError as e:
        return JsonResponse({"error": str(e)}, status=400)
    shift = current_shift(request.user)
    return JsonResponse({"ok": True, "id": sale.pk, "number": sale.number, "total": float(sale.total),
                         "change": float(sale.change_given), "plan": _plan_json(res)["plan"],
                         "drawer": float(cashlib.to_dec(cashlib.total(shift.drawer))) if shift else None})


@login_required
@require_POST
def pos_hold(request):
    try:
        lines, pay, customer, held, car = _parse_cart(request)
        if not lines:
            raise StockError("Чек порожній")
        sale = services.save_held(request.user, lines, pay, customer, current_shift(request.user), held, car=car)
    except StockError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"ok": True, "number": sale.number})


@login_required
def held_list(request):
    qs = Sale.objects.filter(status=Sale.HELD).select_related("customer")
    if not request.user.is_staff:
        qs = qs.filter(cashier=request.user)
    return render(request, "core/held_list.html", {"sales": qs})


@login_required
@require_POST
def held_delete(request, pk):
    s = get_object_or_404(Sale, pk=pk, status=Sale.HELD)
    if s.cashier_id != request.user.id and not request.user.is_staff:
        raise PermissionDenied
    s.delete()
    return redirect("held_list")


# ---------- Sales ----------
@login_required
def sale_list(request):
    qs = done_sales().select_related("cashier", "customer")
    if not request.user.is_staff:
        qs = qs.filter(cashier=request.user)
    d1, d2 = get_date(request, "from", None), get_date(request, "to", None)
    if d1:
        qs = qs.filter(created_at__date__gte=d1)
    if d2:
        qs = qs.filter(created_at__date__lte=d2)
    if request.GET.get("payment"):
        qs = qs.filter(payment_method=request.GET["payment"])
    if request.user.is_staff and request.GET.get("cashier"):
        qs = qs.filter(cashier_id=request.GET["cashier"])
    from django.contrib.auth import get_user_model
    from .designer_views import print_bar
    return render(request, "core/sale_list.html", {
        "sales": qs.annotate(qty=Sum("items__quantity"))[:300],
        "cashiers": get_user_model().objects.all(), "payments": Sale.PAYMENTS, **print_bar("receipt")})


@login_required
def sale_detail(request, pk):
    sale = get_object_or_404(Sale.objects.select_related("cashier", "customer"), pk=pk)
    if not request.user.is_staff and sale.cashier_id != request.user.id:
        raise PermissionDenied
    return render(request, "core/sale_detail.html", {"sale": sale, "items": sale.items.select_related("product"),
                                                     "returns": sale.returns.all()})


@staff_only
@require_POST
def sale_return(request, pk):
    sale = get_object_or_404(Sale, pk=pk, status=Sale.DONE)
    qty_map = {}
    for item in sale.items.all():
        q = dec(request.POST.get(f"ret_{item.pk}", "0"), D0)
        if q and q > 0:
            qty_map[item.pk] = q
    try:
        ret = services.create_return(request.user, sale, qty_map)
        messages.success(request, f"Повернення {ret.number} на суму {ret.total} ₴ проведено")
    except StockError as e:
        messages.error(request, str(e))
    return redirect("sale_detail", pk=pk)


# ---------- Inventory ----------
@staff_only
def inventory(request):
    qs = Product.objects.select_related("category").filter(is_active=True)
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
    return render(request, "core/inventory.html", {"products": qs, "categories": Category.objects.all(),
                                                   "q": q, "cat": cat, "st": st})


@staff_only
def inventory_adjust(request):
    form = AdjustForm(request.POST or None)
    form.fields["product"].queryset = Product.objects.filter(is_active=True)
    if request.method == "POST" and form.is_valid():
        try:
            services.apply_adjustment(request.user, form)
            messages.success(request, "Коригування проведено")
            return redirect("inventory_movements")
        except StockError as e:
            messages.error(request, str(e))
    return render(request, "core/form.html", {"form": form, "title": "Коригування запасів", "back": "inventory"})


@staff_only
def inventory_movements(request):
    qs = StockMovement.objects.select_related("product", "user")
    if request.GET.get("product"):
        qs = qs.filter(product_id=request.GET["product"])
    if request.GET.get("type"):
        qs = qs.filter(movement_type=request.GET["type"])
    d1, d2 = get_date(request, "from", None), get_date(request, "to", None)
    if d1:
        qs = qs.filter(created_at__date__gte=d1)
    if d2:
        qs = qs.filter(created_at__date__lte=d2)
    return render(request, "core/movements.html", {"moves": qs[:300], "products": Product.objects.all(),
                                                   "types": StockMovement.TYPES})


# ---------- Customers ----------
@staff_only
def customer_list(request):
    qs = Customer.objects.annotate(last=Max("sales__created_at", filter=Q(sales__status="done")))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name_lc__contains=q.lower()) | Q(phone__icontains=q))
    return render(request, "core/customer_list.html", {"customers": qs, "q": q})


def customer_edit(request, pk=None):
    return simple_form(request, CustomerForm, "Клієнт", "customer_list", pk, Customer)


@staff_only
def customer_detail(request, pk):
    c = get_object_or_404(Customer, pk=pk)
    sales = c.sales.filter(status="done").prefetch_related("items__product")
    return render(request, "core/customer_detail.html", {"c": c, "sales": sales})


# ---------- Reports ----------
NET = ExpressionWrapper(F("quantity") - F("returned_qty"), output_field=DEC)


def _items(d1, d2):
    return SaleItem.objects.filter(sale__status="done", sale__created_at__date__range=(d1, d2))


@staff_only
def report_daily(request):
    d = get_date(request, "date", timezone.localdate())
    sales = done_sales().filter(created_at__date=d)
    split = sales.aggregate(c=Sum("cash_amount"), k=Sum("card_amount"))
    items = (_items(d, d).values("product__name", "product__sku")
             .annotate(q=Sum("quantity"), s=Sum(ExpressionWrapper(F("quantity") * F("price"), output_field=DEC)))
             .order_by("-s"))
    return render(request, "core/report_daily.html", {
        "d": d, "total": sales.aggregate(s=Sum("total"))["s"] or D0, "count": sales.count(),
        "units": _items(d, d).aggregate(s=Sum("quantity"))["s"] or D0,
        "cash": split["c"] or D0, "card": split["k"] or D0, "items": items})


@staff_only
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
        qs = done_sales().filter(created_at__year=f.year, created_at__month=f.month)
        return qs, qs.aggregate(s=Sum("total"))["s"] or D0
    qs, total = agg(first)
    _, prev_total = agg(prev_first)
    days = qs.annotate(day=TruncDate("created_at")).values("day").annotate(s=Sum("total"), n=Count("id")).order_by("day")
    nd = max(len(days), 1)
    pct = ((total - prev_total) / prev_total * 100) if prev_total else None
    return render(request, "core/report_monthly.html", {
        "first": first, "total": total, "avg": total / nd, "count": qs.count(), "days": days,
        "prev_total": prev_total, "diff": total - prev_total, "pct": pct})


@staff_only
def report_top(request):
    today = timezone.localdate()
    d1, d2 = get_date(request, "from", today.replace(day=1)), get_date(request, "to", today)
    n = int(request.GET.get("n", 10)) if request.GET.get("n", "10").isdigit() else 10
    base = (_items(d1, d2).values("product__name", "product__sku")
            .annotate(q=Sum(NET), s=Sum(ExpressionWrapper(NET * F("price"), output_field=DEC)), n=Count("sale", distinct=True)))
    return render(request, "core/report_top.html", {"d1": d1, "d2": d2, "n": n,
                                                    "by_sum": base.order_by("-s")[:n], "by_qty": base.order_by("-q")[:n]})


@staff_only
def report_profit(request):
    today = timezone.localdate()
    d1, d2 = get_date(request, "from", today.replace(day=1)), get_date(request, "to", today)
    # Margin is realized only after the item is handed over to the customer.
    # Supplier-order sales remain in revenue reports, but are not counted as earned
    # profit until fulfilment sets earned_at; returns reduce both net revenue and cost.
    rows = list(_items(d1, d2).filter(earned_at__isnull=False)
        .values("product__name", "product__sku").annotate(
            q=Sum(NET), rev=Sum(ExpressionWrapper(NET * F("price"), output_field=DEC)),
            cost=Sum(ExpressionWrapper(NET * F("cost_price"), output_field=DEC))).order_by("-rev"))
    for r in rows:
        r["margin"] = (r["rev"] or D0) - (r["cost"] or D0)
        r["pct"] = r["margin"] / r["rev"] * 100 if r["rev"] else D0
    rev, cost = sum((r["rev"] or D0 for r in rows), D0), sum((r["cost"] or D0 for r in rows), D0)
    return render(request, "core/report_profit.html", {"d1": d1, "d2": d2, "rows": rows, "rev": rev,
                                                       "cost": cost, "margin": rev - cost})


@staff_only
def report_stock(request):
    value = Product.objects.aggregate(v=Sum(ExpressionWrapper(F("current_stock") * F("cost_price"), output_field=DEC)))["v"] or D0
    summary = StockMovement.objects.values("movement_type").annotate(s=Sum("quantity_change"), n=Count("id"))
    names = dict(StockMovement.TYPES)
    for r in summary:
        r["label"] = names.get(r["movement_type"], r["movement_type"])
    return render(request, "core/report_stock.html", {
        "value": value, "summary": summary,
        "low": Product.objects.filter(is_active=True, current_stock__lte=F("min_stock"))})


# ---------- Shifts ----------
def shift_summary(shift):
    qs = done_sales().filter(shift=shift)
    a = qs.aggregate(t=Sum("total"), c=Sum("cash_amount"), k=Sum("card_amount"), ch=Sum("change_given"))
    return {"total": a["t"] or D0, "count": qs.count(), "cash": a["c"] or D0, "card": a["k"] or D0,
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


@login_required
@transaction.atomic
def shift_view(request):
    shift = current_shift(request.user)
    if request.method == "POST":
        # Serialize open/close actions for this cashier and refresh the shift under lock.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        shift = Shift.objects.select_for_update().filter(
            cashier=request.user, closed_at__isnull=True
        ).first()
        action = request.POST.get("action")
        if action == "open" and not shift:
            counts = _counts_from_post(request, "c_", {})
            cash_total = cashlib.to_dec(cashlib.total(counts))
            if not counts and dec(request.POST.get("cash")):
                cash_total = dec(request.POST.get("cash"), D0)
            Shift.objects.create(cashier=request.user, opening_cash=cash_total,
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
    last = Shift.objects.filter(cashier=request.user, closed_at__isnull=False).first()
    prefill = (last.closing_counted if last and last.closing_counted is not None else (last.drawer if last else {}))
    ctx = {"shift": shift, "summary": shift_summary(shift) if shift else None,
           "history": Shift.objects.filter(cashier=request.user)[:10]}
    ctx["rows"] = _drawer_rows(shift.drawer if shift else prefill)
    return render(request, "core/shift.html", ctx)


@login_required
@transaction.atomic
def drawer_view(request):
    shift = current_shift(request.user)
    if request.method == "POST":
        # A checkout and a drawer adjustment must never overwrite each other's counts.
        shift = Shift.objects.select_for_update().filter(
            cashier=request.user, closed_at__isnull=True
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
                shift=shift, user=request.user, kind=kind,
                amount=cashlib.to_dec(delta_kop), note=request.POST.get("note", "")[:200],
                detail={str(k): new.get(k, 0) - old.get(k, 0) for k in cashlib.DENOMS if new.get(k, 0) != old.get(k, 0)})
            shift.drawer = cashlib.store(new)
            shift.save(update_fields=["drawer"])
            messages.success(request, "Касу оновлено")
        return redirect("drawer")
    return render(request, "core/drawer.html", {"shift": shift, "rows": _drawer_rows(shift.drawer),
                                                "total": cashlib.to_dec(cashlib.total(shift.drawer)),
                                                "ops": shift.cash_ops.all()[:20]})

# ---------- Settings ----------
@staff_only
def settings_home(request):
    return redirect("designer_home")


@staff_only
def company_settings(request):
    from django.forms import modelformset_factory
    AccFS = modelformset_factory(BankAccount, fields=["bank_name", "iban", "mfo", "holder", "currency", "note", "is_default"],
                                 extra=1, can_delete=True)
    obj = CompanySettings.get()
    form = CompanyForm(request.POST or None, request.FILES or None, instance=obj)
    fs = AccFS(request.POST or None, queryset=BankAccount.objects.all())
    if request.method == "POST" and form.is_valid() and fs.is_valid():
        form.save()
        for inst in fs.save(commit=False):
            inst.save()
        for inst in fs.deleted_objects:
            inst.delete()
        messages.success(request, "Налаштування збережено")
        return redirect("company_settings")
    return render(request, "core/company_settings.html", {"form": form, "fs": fs})
