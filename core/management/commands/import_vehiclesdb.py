"""Import the open VehiclesDB make/model catalogue (not parts fitment).
Usage: python manage.py import_vehiclesdb [--url URL] [--dry-run]
Data is CC BY 4.0; preserve visible attribution in the UI.
"""
import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import CatalogImportRun, CatalogSource, VehicleMake, VehicleModel

DEFAULT_URL = "https://cdn.jsdelivr.net/gh/vehiclesdb/vehiclesdb@v2026.10.3/dist/vehicles.json"
SOURCE_NAME = "VehiclesDB open dataset"


def slugify(value):
    import re
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "unknown"


def get_kind(model, fallback="car"):
    kind = model.get("kind") or fallback
    aliases = {"cars": "car", "motorcycles": "motorcycle", "mopeds": "moped",
               "vans": "van", "trucks": "truck", "buses": "bus"}
    return aliases.get(str(kind).lower(), str(kind).lower())


class Command(BaseCommand):
    help = "Імпортує довідник марок і моделей VehiclesDB; повторний запуск не створює дублікатів."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=DEFAULT_URL)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--timeout", type=int, default=60)

    def handle(self, *args, **opts):
        url = opts["url"]
        try:
            req = Request(url, headers={"User-Agent": "crm-prototup/vehicle-catalog-import"})
            with urlopen(req, timeout=opts["timeout"]) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            raise CommandError(f"Не вдалося завантажити JSON каталогу: {exc}") from exc

        version = str(payload.get("version") or "unknown") if isinstance(payload, dict) else "unknown"
        source, _ = CatalogSource.objects.get_or_create(
            name=SOURCE_NAME,
            defaults={
                "url": "https://github.com/vehiclesdb/vehiclesdb",
                "license_name": "CC BY 4.0",
                "attribution": "Vehicle data by VehiclesDB — https://vehiclesdb.com",
                "version": version,
                "commercial_use": True,
                "notes": "Відкритий довідник марок/моделей. Не містить каталогу запчастин або підтвердженої застосовності.",
            },
        )
        # Refresh metadata without overwriting a manually pinned local version.
        if source.version != version and version != "unknown":
            source.version = version
            source.url = "https://github.com/vehiclesdb/vehiclesdb"
            source.save(update_fields=["version", "url"])

        makes = payload.get("makes", []) if isinstance(payload, dict) else []
        if not makes and isinstance(payload, dict) and isinstance(payload.get("vehicles"), list):
            makes = payload["vehicles"]
        if not isinstance(makes, list):
            raise CommandError("Формат каталогу не розпізнано: очікувався JSON зі списком makes.")

        run = CatalogImportRun.objects.create(source=source)
        created = updated = skipped = errors = 0
        try:
            with transaction.atomic():
                for make_row in makes:
                    if not isinstance(make_row, dict):
                        skipped += 1
                        continue
                    make_name = str(make_row.get("name") or "").strip()
                    make_slug = str(make_row.get("slug") or slugify(make_name)).strip()
                    if not make_name:
                        skipped += 1
                        continue
                    model_rows = make_row.get("models") or []
                    if not isinstance(model_rows, list):
                        skipped += 1
                        continue
                    # VehiclesDB can provide different kinds under one make.
                    kinds = sorted({get_kind(m) for m in model_rows if isinstance(m, dict)}) or ["car"]
                    for kind in kinds:
                        make_obj, was_created = VehicleMake.objects.get_or_create(
                            kind=kind, slug=make_slug,
                            defaults={"name": make_name, "source": source,
                                      "source_id": f"{kind}/{make_slug}", "name_norm": make_name.casefold()},
                        )
                        if was_created:
                            created += 1
                        elif make_obj.name != make_name or make_obj.name_norm != make_name.casefold():
                            make_obj.name = make_name
                            make_obj.name_norm = make_name.casefold()
                            make_obj.save(update_fields=["name", "name_norm"])
                            updated += 1
                    by_kind = {x.kind: x for x in VehicleMake.objects.filter(kind__in=kinds, slug=make_slug)}
                    for model_row in model_rows:
                        if not isinstance(model_row, dict):
                            skipped += 1
                            continue
                        model_name = str(model_row.get("name") or "").strip()
                        model_slug = str(model_row.get("slug") or slugify(model_name)).strip()
                        kind = get_kind(model_row)
                        vehicle_make = by_kind.get(kind)
                        if not model_name or not vehicle_make:
                            skipped += 1
                            continue
                        external_id = str(model_row.get("id") or f"{kind}/{make_slug}/{model_slug}")
                        defaults = {
                            "name": model_name,
                            "source_id": external_id,
                            "body_types": model_row.get("body_types") or [],
                            "metadata": {
                                key: model_row[key] for key in ("availability", "popularity", "sources", "xrefs", "years")
                                if key in model_row
                            },
                        }
                        obj, was_created = VehicleModel.objects.get_or_create(
                            make=vehicle_make, slug=model_slug, defaults=defaults,
                        )
                        if was_created:
                            created += 1
                        else:
                            changed = []
                            for field in ("name", "source_id", "body_types", "metadata"):
                                if getattr(obj, field) != defaults[field]:
                                    setattr(obj, field, defaults[field])
                                    changed.append(field)
                            if changed:
                                obj.save(update_fields=changed)
                                updated += 1
                            else:
                                skipped += 1
                if opts["dry_run"]:
                    transaction.set_rollback(True)
            run.status = "success"
            run.created_count = created
            run.updated_count = updated
            run.skipped_count = skipped
            run.error_count = errors
            run.message = "Dry run — усі зміни відкочено." if opts["dry_run"] else f"Джерело: {url}"
        except Exception as exc:
            run.status = "failed"
            run.error_count = errors + 1
            run.message = str(exc)[:4000]
            raise
        finally:
            run.finished_at = timezone.now()
            run.save()
        self.stdout.write(self.style.SUCCESS(
            f"VehiclesDB: створено {created}, оновлено {updated}, без змін/пропущено {skipped}; "
            + ("dry-run" if opts["dry_run"] else "збережено")
        ))
