"""Import licensed parts/fitment CSV without duplicates.
Columns: brand,sku,name,category,oe,make_slug,model_slug,kind,year_from,year_to,engine_code.
Optional: source_id,verified,description,license_name,attribution.
This command never changes Product stock or prices.
"""
import csv
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from core.models import (CatalogPart, CatalogPartNumber, CatalogSource, PartFitment,
                         PartOe, VehicleMake, VehicleModel, norm_sku)


class Command(BaseCommand):
    help = "Імпорт дозволеного каталогу деталей/застосовності з CSV з дедуплікацією."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Шлях до CSV UTF-8")
        parser.add_argument("--source", required=True, help="Унікальна назва джерела")
        parser.add_argument("--url", default="")
        parser.add_argument("--license", default="")
        parser.add_argument("--attribution", default="")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        path = Path(opts["file"])
        if not path.is_file():
            raise CommandError(f"Файл не знайдено: {path}")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CommandError(f"Не вдалося прочитати CSV: {exc}") from exc
        if not rows:
            self.stdout.write("CSV порожній; імпортовано 0 записів.")
            return
        required = {"brand", "sku"}
        if not required.issubset(set(rows[0])):
            raise CommandError("Обов'язкові колонки CSV: brand,sku. Додатково: name,category,oe,make_slug,model_slug,kind,year_from,year_to,engine_code.")
        source, _ = CatalogSource.objects.get_or_create(
            name=opts["source"],
            defaults={"url": opts["url"], "license_name": opts["license"],
                      "attribution": opts["attribution"], "commercial_use": False,
                      "notes": "Імпорт CSV; перед використанням перевірте права на комерційне використання та поширення."},
        )
        if opts["url"] and source.url != opts["url"]:
            source.url = opts["url"]
        if opts["license"] and source.license_name != opts["license"]:
            source.license_name = opts["license"]
        if opts["attribution"] and source.attribution != opts["attribution"]:
            source.attribution = opts["attribution"]
        source.save()
        created = updated = skipped = errors = 0
        try:
            with transaction.atomic():
                for line_no, row in enumerate(rows, start=2):
                    brand = (row.get("brand") or "").strip()[:60]
                    sku = (row.get("sku") or "").strip()[:80]
                    sn = norm_sku(sku)
                    if not brand or not sn:
                        skipped += 1
                        continue
                    brand_norm = brand.casefold().strip()
                    name = (row.get("name") or "").strip()[:250]
                    number = CatalogPartNumber.objects.filter(brand_norm=brand_norm, sku_norm=sn).first()
                    if number is None:
                        # Only create a logical part after confirming this brand/number is new;
                        # repeated imports must not leave orphan CatalogPart rows behind.
                        part = CatalogPart.objects.create(
                            name=name, category_name=(row.get("category") or "").strip()[:120],
                            description=(row.get("description") or "").strip()[:5000],
                        )
                        number = CatalogPartNumber.objects.create(
                            part=part, brand=brand, brand_norm=brand_norm, sku=sku, sku_norm=sn,
                            source=source, source_id=(row.get("source_id") or "").strip()[:180],
                            verified=(row.get("verified") or "").strip().lower() in ("1", "true", "yes", "так"),
                            confidence=100 if (row.get("verified") or "").strip().lower() in ("1", "true", "yes", "так") else 60,
                        )
                        created += 1
                    else:
                        # Existing normalized brand+SKU wins; do not silently reassign its identity.
                        if name and not number.part.name:
                            number.part.name = name
                            number.part.save(update_fields=["name", "updated_at"])
                            updated += 1
                        else:
                            skipped += 1
                    oe = (row.get("oe") or "").strip()[:80]
                    if oe:
                        oe_norm = norm_sku(oe)
                        if oe_norm:
                            _, oe_created = PartOe.objects.get_or_create(
                                brand=brand.upper(), sku_norm=sn, oe_norm=oe_norm,
                                defaults={"sku": sku, "oe_raw": oe, "oe_brand": (row.get("oe_brand") or "").strip().upper()[:60], "source": "import"},
                            )
                            if oe_created:
                                created += 1
                    make_slug = (row.get("make_slug") or "").strip()
                    model_slug = (row.get("model_slug") or "").strip()
                    if make_slug and model_slug:
                        kind = (row.get("kind") or "car").strip().lower()
                        vehicle_model = VehicleModel.objects.filter(make__slug=make_slug, make__kind=kind, slug=model_slug).first()
                        if vehicle_model:
                            def year(field):
                                val = (row.get(field) or "").strip()
                                return int(val) if val.isdigit() and 1886 <= int(val) <= 2200 else None
                            year_from, year_to = year("year_from"), year("year_to")
                            engine_code = (row.get("engine_code") or "").strip()[:80]
                            fitment_key = f"v:0|y:{year_from or 0}-{year_to or 0}|e:{engine_code.upper()}"[:180]
                            defaults = {
                                "part_number": number, "vehicle_model": vehicle_model, "variant": None,
                                "source": source, "year_from": year_from, "year_to": year_to,
                                "engine_code": engine_code, "fitment_key": fitment_key,
                                "notes": (row.get("fitment_notes") or "").strip()[:250],
                                "verified": (row.get("verified") or "").strip().lower() in ("1", "true", "yes", "так"),
                            }
                            _, fit_created = PartFitment.objects.get_or_create(**defaults)
                            if fit_created:
                                created += 1
                        else:
                            skipped += 1
                if opts["dry_run"]:
                    transaction.set_rollback(True)
        except Exception as exc:
            errors += 1
            raise CommandError(f"Помилка імпорту (після {created} створень; транзакцію скасовано): {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            f"Імпорт: нових {created}, оновлено {updated}, пропущено {skipped}, помилок {errors}"
            + ("; dry-run — зміни відкочено." if opts["dry_run"] else ".")
        ))
