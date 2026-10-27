"""Підбір деталей за авто: марка/модель/рік → сумісні деталі за застосовністю, згруповані за типом."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from . import fitment, pricing
from .crosses import name_profile
from .models import CustomerCar, PartInfo, Product, SupplierItem


@login_required
def car_parts(request):
    car = get_object_or_404(CustomerCar, pk=request.GET["car"]) if request.GET.get("car", "").isdigit() else None
    make = (car.make if car else request.GET.get("make", "")).strip()
    model = (car.model if car else request.GET.get("model", "")).strip()
    try:
        year = car.year if car else (int(request.GET["year"]) if request.GET.get("year") else None)
    except ValueError:
        year = None
    customer = car.customer if car else None
    tq = request.GET.get("q", "").strip().lower()
    instock = bool(request.GET.get("instock"))
    toks = fitment.car_tokens(make, model)
    ctx = {"car": car, "make": make, "model": model, "year": year or "", "q": request.GET.get("q", ""), "instock": instock,
           "searched": bool(toks), "groups": [], "total": 0, "known": PartInfo.objects.exclude(cars="").count(), "customer": customer}
    if not toks:
        return render(request, "core/car_parts.html", ctx)
    cond = Q()
    for t in toks:
        cond &= Q(cars_lc__contains=t)
    keys = set()
    for pi in PartInfo.objects.filter(cond)[:4000]:
        if any(fitment.line_matches(ln, toks, year) for ln in pi.cars_lc.splitlines()):
            keys.add((pi.brand, pi.sku_norm))
    norms = {n for _, n in keys}
    own = {}
    for p in Product.objects.filter(sku_norm__in=norms, is_active=True).select_related("category"):
        own.setdefault((p.brand.upper(), p.sku_norm), p)
    offers = {}
    for o in SupplierItem.objects.filter(sku_norm__in=norms, available=True).select_related("supplier", "product").order_by("-cost"):
        offers[(o.brand, o.sku_norm)] = o                         # останній у циклі = найдешевший
    rs = pricing.RuleSet()
    groups, labels = {}, {}
    for key in keys:
        p, o = own.get(key), offers.get(key)
        if not p and not o:
            continue
        name = (p.name if p else o.name) or key[1]
        if tq and tq not in name.lower() and tq not in key[0].lower() and tq not in key[1].lower():
            continue
        stock = p.current_stock if p else 0
        if instock and not stock > 0:
            continue
        # група = тип деталі з назви (однаково для товарів зі складу й з прайсів); категорія товару лише запасний варіант
        typ = name_profile(name, key[0])["type"]
        cat = typ[:6] if typ else ((p.category.name if p and p.category_id else "інше").lower()[:6])
        price_for_you = None
        if o:
            price_for_you = pricing.list_price(o.cost, o.brand, (p.category_id if p else None), customer, rs)[0]
        labels.setdefault(cat, typ.capitalize() if typ else ((p.category.name if p and p.category_id else "Інше")))
        groups.setdefault(cat, []).append({"brand": key[0], "sku": (p.sku if p else o.sku), "sn": key[1], "name": name, "stock": stock,
                                           "price": p.price if p else None, "offer": o, "offer_price": price_for_you})
    out = []
    for cat in sorted(groups, key=lambda c: labels[c]):
        rows = sorted(groups[cat], key=lambda r: (not r["stock"] > 0, r["offer_price"] if r["offer_price"] is not None else (r["price"] or 10 ** 9)))
        out.append({"name": labels[cat], "rows": rows})
    ctx.update(groups=out, total=sum(len(g["rows"]) for g in out))
    return render(request, "core/car_parts.html", ctx)
