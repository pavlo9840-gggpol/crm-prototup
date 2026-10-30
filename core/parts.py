"""Картка запчастини (аналог TecDoc-картки), ручне редагування даних, імпорт OE-номерів і описів."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import crosses, enrich
from .apisync import SyncError
from .models import (CatalogPart, CatalogPartNumber, CatalogSource, PartFitment, PartInfo, PartOe, Product, SupplierItem, VehicleMake, VehicleModel, norm_sku)
from .transit import MAX_FILE, TableError, read_table
from .views import staff_only


def _key(request):
    brand = (request.GET.get("b") or request.POST.get("b") or "").strip().upper()
    sku_norm = norm_sku(request.GET.get("s") or request.POST.get("s") or "")
    return brand, sku_norm


@login_required
def part_card(request):
    brand, sn = _key(request)
    if not sn:
        return redirect("pos")
    prods = list(Product.objects.filter(sku_norm=sn, brand__iexact=brand))
    offers = list(SupplierItem.objects.filter(sku_norm=sn, brand=brand).select_related("supplier__currency").order_by("-available", "cost"))
    info = PartInfo.objects.filter(brand=brand, sku_norm=sn).first()
    oes = list(PartOe.objects.filter(brand=brand, sku_norm=sn).order_by("oe_brand", "oe_raw"))
    if not (prods or offers or info or oes):
        messages.error(request, "Такої деталі не знайдено")
        return redirect("pos")
    title = (prods[0].name if prods else offers[0].name if offers else "") or ""
    sku = (prods[0].sku if prods else offers[0].sku if offers else sn)
    return render(request, "core/part_card.html", {
        "brand": brand, "sn": sn, "sku": sku, "title": title, "prods": prods, "offers": offers, "info": info, "oes": oes,
        "stock": sum(p.current_stock for p in prods), "analogs": crosses.analogs(brand, sn, with_names=True),
        "can_refresh": any(o.ext_id for o in offers), "oe_text": "\n".join(f"{o.oe_raw};{o.oe_brand}" if o.oe_brand else o.oe_raw for o in oes if o.source == "manual")})


@login_required
@require_POST
def part_refresh(request):
    brand, sn = _key(request)
    items = SupplierItem.objects.filter(sku_norm=sn, brand=brand).exclude(ext_id="").select_related("supplier__api")
    last_err = "Немає постачальника з налаштованим API для цієї деталі"
    for si in items:
        try:
            info = enrich.enrich_item(si)
            messages.success(request, f"Дані оновлено з API «{si.supplier.name}»: OE-кодів {len(info['oe'])}"
                                      + (", є опис" if info["description"] else ", опису немає"))
            break
        except SyncError as e:
            last_err = str(e)
    else:
        messages.error(request, last_err)
    return redirect(f"/part/?b={brand}&s={sn}")


@staff_only
@require_POST
def part_edit(request):
    brand, sn = _key(request)
    sku = request.POST.get("sku", sn)[:80]
    with transaction.atomic():
        pi, _ = PartInfo.objects.get_or_create(brand=brand, sku_norm=sn, defaults={"sku": sku})
        pi.description = request.POST.get("description", "").strip()[:5000]
        pi.attrs = request.POST.get("attrs", "").strip()[:5000]
        pi.cars = request.POST.get("cars", "").strip()[:8000]
        pi.image_url = request.POST.get("image_url", "").strip()[:400]
        pi.source = "manual"
        pi.save()
        PartOe.objects.filter(brand=brand, sku_norm=sn, source="manual").delete()
        have = set(PartOe.objects.filter(brand=brand, sku_norm=sn).values_list("oe_norm", flat=True))
        rows = []
        for line in request.POST.get("oe", "").splitlines():
            code, _, ob = line.partition(";")
            n = norm_sku(code)
            if n and n not in have and len(code.strip()) <= 80:
                have.add(n)
                rows.append(PartOe(brand=brand, sku_norm=sn, sku=sku, oe_norm=n, oe_raw=code.strip(), oe_brand=ob.strip().upper()[:60], source="manual"))
        PartOe.objects.bulk_create(rows)
    messages.success(request, "Дані про деталь збережено")
    return redirect(f"/part/?b={brand}&s={sn}")


@staff_only
def parts_import(request):
    if request.method == "POST":
        kind = request.POST.get("kind")
        f = request.FILES.get("file")
        try:
            if not f or f.size > MAX_FILE:
                raise TableError("Оберіть файл .xlsx або .csv (до 30 МБ)")
            rows = read_table(f.read(), f.name)[1 if request.POST.get("has_header") else 0:]
            if kind == "oe":
                n_oe = _import_oe(rows)
                messages.success(request, f"Імпортовано зв'язків «деталь ↔ OE»: {n_oe}")
            elif kind == "fitment":
                n_fit = _import_fitments(rows)
                messages.success(request, f"Імпортовано зв'язків застосовності авто ↔ деталь: {n_fit}")
            else:
                n_inf = _import_desc(rows)
                messages.success(request, f"Оновлено описів: {n_inf}")
        except TableError as e:
            messages.error(request, str(e))
        return redirect("parts_import")
    return render(request, "core/parts_import.html", {"n_oe": PartOe.objects.count(), "n_info": PartInfo.objects.exclude(description="").count(),
                                                      "n_parts": PartOe.objects.values("brand", "sku_norm").distinct().count(),
                                                      "n_fitments": PartFitment.objects.count()})


def _import_oe(rows):
    """Колонки: Бренд; Артикул; OE-номер; Бренд OE (необов'язково)."""
    new, seen = [], set()
    for r in rows:
        r = r + [""] * (4 - len(r))
        brand, sku, oe, ob = r[0].strip().upper()[:60], r[1].strip()[:80], r[2].strip()[:80], r[3].strip().upper()[:60]
        sn, on = norm_sku(sku), norm_sku(oe)
        if brand and sn and on and (brand, sn, on) not in seen:
            seen.add((brand, sn, on))
            new.append(PartOe(brand=brand, sku_norm=sn, sku=sku, oe_norm=on, oe_raw=oe, oe_brand=ob, source="import"))
    before = PartOe.objects.count()
    PartOe.objects.bulk_create(new, ignore_conflicts=True, batch_size=500)
    return PartOe.objects.count() - before



def _import_fitments(rows):
    """Колонки: Марка авто; Модель; Бренд деталі; Артикул; Рік від; Рік до; Код двигуна; Перевірено."""
    imported = 0
    source, _ = CatalogSource.objects.get_or_create(
        name="Імпорт застосовності з файлу",
        defaults={"license_name": "За ліцензією джерела файлу",
                  "notes": "Власний імпорт користувача. Перевірте право на комерційне використання даних."},
    )
    for row in rows:
        row = row + [""] * (8 - len(row))
        make_name, model_name = row[0].strip(), row[1].strip()
        brand, sku = row[2].strip().upper()[:60], row[3].strip()[:80]
        if not (make_name and model_name and brand and sku):
            continue
        make = VehicleMake.objects.filter(name__iexact=make_name, kind__in=["car", "van", "truck"]).first()
        if not make:
            continue
        vehicle_model = VehicleModel.objects.filter(make=make, name__iexact=model_name).first()
        if not vehicle_model:
            continue
        def year_value(value):
            value = value.strip()
            if not value:
                return None
            if not value.isdigit() or not 1886 <= int(value) <= 2200:
                raise TableError(f"Некоректний рік у рядку: {value}")
            return int(value)
        year_from, year_to = year_value(row[4]), year_value(row[5])
        if year_from and year_to and year_from > year_to:
            raise TableError(f"Початковий рік більший за кінцевий: {year_from}–{year_to}")
        number = norm_sku(sku)
        part, _ = CatalogPart.objects.get_or_create(name=sku)
        part_number, _ = CatalogPartNumber.objects.get_or_create(
            brand_norm=brand.casefold(), sku_norm=number,
            defaults={"part": part, "brand": brand, "sku": sku, "source": source},
        )
        verified = row[7].strip().lower() in ("1", "true", "yes", "так", "підтверджено")
        fitment, created = PartFitment.objects.get_or_create(
            part_number=part_number, vehicle_model=vehicle_model, source=source,
            year_from=year_from, year_to=year_to, engine_code=row[6].strip()[:80],
            defaults={"verified": verified},
        )
        if created:
            imported += 1
        elif verified and not fitment.verified:
            fitment.verified = True
            fitment.save(update_fields=["verified"])
    return imported


def _import_desc(rows):
    """Колонки: Бренд; Артикул; Опис; Характеристики (необов'язково); Застосовність (необов'язково)."""
    n = 0
    for r in rows:
        r = r + [""] * (5 - len(r))
        brand, sku, desc, attrs, cars = r[0].strip().upper()[:60], r[1].strip()[:80], r[2].strip(), r[3].strip(), r[4].strip()
        sn = norm_sku(sku)
        if not (brand and sn and (desc or attrs or cars)):
            continue
        pi, _ = PartInfo.objects.get_or_create(brand=brand, sku_norm=sn, defaults={"sku": sku, "source": "import"})
        for fld, v in (("description", desc), ("attrs", attrs.replace("|", "\n")), ("cars", cars.replace("|", "\n"))):
            if v and (pi.source != "manual" or not getattr(pi, fld)):
                setattr(pi, fld, v[:8000])
        pi.save()
        n += 1
    return n


@login_required
@require_POST
def analog_action(request):
    """Менеджер підтверджує аналог (усі) або визнає, що це НЕ аналог (лише адміністратор)."""
    brand, sn = _key(request)
    other = norm_sku(request.POST.get("os", ""))
    act = request.POST.get("act")
    if not (sn and other and sn != other):
        messages.error(request, "Некоректна пара")
    elif act == "confirm":
        crosses.confirm_pair(request.user, sn, other)
        messages.success(request, "Аналог підтверджено: тепер він має найвищу довіру")
    elif act == "reject" and request.user.is_staff:
        crosses.reject_pair(request.user, sn, other)
        messages.success(request, "Позначено «не аналог»: пару приховано з усіх джерел")
    else:
        messages.error(request, "Дія недоступна")
    return redirect(f"/part/?b={brand}&s={sn}")
