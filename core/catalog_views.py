"""Unified vehicle/parts catalogue UI; does not mutate stock or sale records."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render

from .models import (CatalogPartNumber, CatalogSource, PartFitment, PartOe,
                     Product, SupplierItem, VehicleMake, VehicleModel, norm_sku)


@login_required
def catalogue_home(request):
    kind = (request.GET.get("kind") or "car").strip().lower()
    make_id = request.GET.get("make", "").strip()
    model_id = request.GET.get("model", "").strip()
    query = (request.GET.get("q") or "").strip()[:100]
    year_text = (request.GET.get("year") or "").strip()
    year = int(year_text) if year_text.isdigit() and 1886 <= int(year_text) <= 2200 else None

    makes = VehicleMake.objects.filter(kind=kind).order_by("name")
    models = VehicleModel.objects.filter(make_id=make_id, make__kind=kind).order_by("name") if make_id.isdigit() else VehicleModel.objects.none()
    selected_model = VehicleModel.objects.filter(pk=model_id, make__kind=kind).select_related("make").first() if model_id.isdigit() else None

    parts = CatalogPartNumber.objects.select_related("part", "source", "product").all()
    searched = bool(query or selected_model)
    if query:
        qn = norm_sku(query)
        oe_norms = set(PartOe.objects.filter(oe_norm=qn).values_list("sku_norm", flat=True))
        # SKU/brand exact normalized search first; text search is an additional fallback.
        parts = parts.filter(
            Q(sku_norm=qn) | Q(brand_norm=query.casefold()) | Q(sku_norm__in=oe_norms) |
            Q(part__name__icontains=query) | Q(part__description__icontains=query)
        )
    if selected_model:
        fitment_ids = PartFitment.objects.filter(vehicle_model=selected_model)
        if year:
            fitment_ids = fitment_ids.filter(Q(year_from__isnull=True) | Q(year_from__lte=year)).filter(
                Q(year_to__isnull=True) | Q(year_to__gte=year))
        allowed = fitment_ids.values_list("part_number_id", flat=True)
        parts = parts.filter(pk__in=allowed)

    rows = []
    if searched:
        for number in parts.order_by("brand", "sku")[:300]:
            product = number.product
            if not product:
                product = Product.objects.filter(brand__iexact=number.brand, sku_norm=number.sku_norm, is_active=True).first()
            offer = SupplierItem.objects.filter(brand__iexact=number.brand, sku_norm=number.sku_norm, available=True).select_related("supplier").order_by("cost").first()
            evidence = PartFitment.objects.filter(part_number=number).select_related("source", "vehicle_model__make")
            if selected_model:
                evidence = evidence.filter(vehicle_model=selected_model)
            rows.append({
                "number": number, "product": product, "offer": offer,
                "fitments": list(evidence[:3]),
                "stock": product.current_stock if product else 0,
                "price": product.price if product else None,
            })

    context = {
        "kind": kind, "makes": makes, "models": models, "selected_model": selected_model,
        "make_id": make_id, "model_id": model_id, "query": query, "year": year or "",
        "searched": searched, "rows": rows,
        "n_makes": VehicleMake.objects.count(), "n_models": VehicleModel.objects.count(),
        "n_parts": CatalogPartNumber.objects.count(), "n_fitments": PartFitment.objects.count(),
        "n_sources": CatalogSource.objects.count(),
        "attribution": "Vehicle data by VehiclesDB",
        "attribution_url": "https://vehiclesdb.com",
    }
    return render(request, "core/catalogue.html", context)
