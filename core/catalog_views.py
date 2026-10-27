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

    catalog_qs = CatalogPartNumber.objects.select_related("part", "source", "product").all()
    fitment_ids = None
    if selected_model:
        fitments = PartFitment.objects.filter(vehicle_model=selected_model)
        if year:
            fitments = fitments.filter(Q(year_from__isnull=True) | Q(year_from__lte=year)).filter(
                Q(year_to__isnull=True) | Q(year_to__gte=year))
        fitment_ids = set(fitments.values_list("part_number_id", flat=True))
        catalog_qs = catalog_qs.filter(pk__in=fitment_ids)

    searched = bool(query or selected_model)
    qn = norm_sku(query) if query else ""
    oe_norms = set(PartOe.objects.filter(oe_norm=qn).values_list("sku_norm", flat=True)) if qn else set()
    if query:
        catalog_qs = catalog_qs.filter(
            Q(sku_norm=qn) | Q(brand_norm=query.casefold()) | Q(sku_norm__in=oe_norms) |
            Q(part__name__icontains=query) | Q(part__description__icontains=query)
        )

    rows_by_key = {}
    if searched:
        for number in catalog_qs.order_by("brand", "sku")[:300]:
            product = number.product or Product.objects.filter(
                brand__iexact=number.brand, sku_norm=number.sku_norm, is_active=True
            ).first()
            offer = SupplierItem.objects.filter(
                brand__iexact=number.brand, sku_norm=number.sku_norm, available=True
            ).select_related("supplier").order_by("cost").first()
            evidence = PartFitment.objects.filter(part_number=number).select_related(
                "source", "vehicle_model__make"
            )
            if selected_model:
                evidence = evidence.filter(vehicle_model=selected_model)
            key = (number.brand.casefold(), number.sku_norm)
            rows_by_key[key] = {
                "brand": number.brand, "sku": number.sku, "name": number.part.name,
                "number": number, "verified": number.verified, "source": number.source.name,
                "product": product, "offer": offer, "fitments": list(evidence[:3]),
                "stock": product.current_stock if product else 0,
                "price": product.price if product else None,
            }

        # Manual CRM products and supplier offers are searchable even before a
        # separate parts-catalogue record exists. They are not shown as vehicle-
        # compatible unless a fitment record proves that relationship.
        if query and not selected_model:
            product_qs = Product.objects.filter(is_active=True).filter(
                Q(sku_norm=qn) | Q(brand__iexact=query) | Q(name_lc__contains=query.lower())
            ).select_related("category")[:150]
            for product in product_qs:
                key = ((product.brand or "").casefold(), product.sku_norm)
                if key not in rows_by_key:
                    number = CatalogPartNumber.objects.filter(brand_norm=key[0], sku_norm=key[1]).select_related("part", "source").first()
                    offer = SupplierItem.objects.filter(brand__iexact=product.brand, sku_norm=product.sku_norm, available=True).select_related("supplier").order_by("cost").first()
                    rows_by_key[key] = {
                        "brand": product.brand, "sku": product.sku, "name": product.name,
                        "number": number, "verified": bool(number and number.verified),
                        "source": number.source.name if number else "CRM: товар",
                        "product": product, "offer": offer, "fitments": [],
                        "stock": product.current_stock, "price": product.price,
                    }
            offers_qs = SupplierItem.objects.filter(available=True).filter(
                Q(sku_norm=qn) | Q(brand__iexact=query) | Q(name_lc__contains=query.lower())
            ).select_related("supplier", "product").order_by("cost")[:150]
            for offer in offers_qs:
                key = ((offer.brand or "").casefold(), offer.sku_norm)
                if key not in rows_by_key:
                    product = offer.product or Product.objects.filter(
                        brand__iexact=offer.brand, sku_norm=offer.sku_norm, is_active=True
                    ).first()
                    number = CatalogPartNumber.objects.filter(brand_norm=key[0], sku_norm=key[1]).select_related("part", "source").first()
                    rows_by_key[key] = {
                        "brand": offer.brand, "sku": offer.sku, "name": offer.name,
                        "number": number, "verified": bool(number and number.verified),
                        "source": number.source.name if number else "CRM: прайс постачальника",
                        "product": product, "offer": offer, "fitments": [],
                        "stock": product.current_stock if product else 0,
                        "price": product.price if product else None,
                    }

    rows = sorted(rows_by_key.values(), key=lambda r: (not bool(r["stock"]), r["brand"].casefold(), r["sku"]))[:300]
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
