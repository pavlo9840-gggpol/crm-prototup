"""Safely normalize a legacy AutoZapaz database to one local store.

Default mode is read-only. Applying changes requires an explicit --apply flag
and a verified SQLite backup path. Multiple legacy stores are deliberately
rejected: this command never guesses how to merge their business records.
"""
import json
import sqlite3
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.models import F, Q
from django.db.models.fields.related import ForeignKey


def company_fk(model, company_model):
    for field in model._meta.fields:
        if isinstance(field, ForeignKey) and field.remote_field.model == company_model:
            return field
    return None


def company_scoped_models(company_model):
    result = []
    for model in apps.get_models(include_auto_created=False):
        if model._meta.app_label != "core" or model is company_model or model.__name__ == "CompanyMembership":
            continue
        field = company_fk(model, company_model)
        if field:
            result.append((model, field))
    return sorted(result, key=lambda item: item[0]._meta.label_lower)


def cross_company_reference_mismatches(model, source_company_field, alias, company_model):
    """Count rows whose FK points to a record owned by a different store."""
    total = 0
    source_attname = source_company_field.attname
    for relation in model._meta.fields:
        if not isinstance(relation, ForeignKey) or relation.remote_field.model is company_model:
            continue
        target_company_field = company_fk(relation.remote_field.model, company_model)
        if not target_company_field:
            continue
        target_lookup = f"{relation.name}__{target_company_field.attname}"
        # NULL ownership is normalized to the one local store by the
        # apply phase. Only two different, explicit store IDs are ambiguous.
        mismatch = (
            Q(**{f"{source_attname}__isnull": False})
            & Q(**{f"{target_lookup}__isnull": False})
            & ~Q(**{source_attname: F(target_lookup)})
        )
        total += model.objects.using(alias).filter(mismatch).count()
    return total


def company_unique_key_collisions(model, company_field, store_id, alias):
    """Find uniqueness collisions that would appear when NULL store IDs are filled."""
    collisions = []
    for constraint in model._meta.constraints:
        fields = getattr(constraint, "fields", None)
        if not fields or company_field.name not in fields:
            continue
        key_fields = [model._meta.get_field(name) for name in fields if name != company_field.name]
        null_qs = model.objects.using(alias).filter(**{f"{company_field.name}__isnull": True})
        target_qs = model.objects.using(alias).filter(**{company_field.name: store_id})

        # Respect the existing partial unique constraints on barcode and payroll rows.
        if constraint.name == "uniq_company_product_barcode":
            null_qs = null_qs.exclude(barcode__isnull=True).exclude(barcode="")
        elif constraint.name == "uniq_company_payplan_user":
            null_qs = null_qs.filter(user__isnull=False)
            target_qs = target_qs.filter(user__isnull=False)
        elif constraint.name == "uniq_company_payplan_default":
            null_qs = null_qs.filter(user__isnull=True)
            target_qs = target_qs.filter(user__isnull=True)

        key_names = [field.name for field in key_fields]
        key_attnames = [field.attname for field in key_fields]
        grouped = {}
        for values in null_qs.values_list(*key_attnames).iterator():
            grouped[values] = grouped.get(values, 0) + 1

        for values, null_count in grouped.items():
            lookup = {name: value for name, value in zip(key_names, values)}
            duplicate_in_null_rows = null_count > 1
            duplicate_in_target = target_qs.filter(**lookup).exists()
            if duplicate_in_null_rows or duplicate_in_target:
                collisions.append({
                    "constraint": constraint.name,
                    "key": lookup,
                    "null_rows": null_count,
                    "already_in_store": duplicate_in_target,
                })
    return collisions


def create_sqlite_backup(alias, destination):
    connection = connections[alias]
    if connection.vendor != "sqlite":
        raise CommandError("Автоматичний backup перед цим перенесенням наразі підтримує лише SQLite.")
    database_name = connection.settings_dict.get("NAME")
    if not database_name or str(database_name) == ":memory:" or str(database_name).startswith("file:"):
        raise CommandError("Для тестової/тимчасової SQLite-бази неможливо створити файловий backup.")
    source = Path(database_name).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if source == target:
        raise CommandError("Файл backup не може бути самим файлом бази даних.")
    if target.exists():
        raise CommandError(f"Файл backup вже існує: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise CommandError(f"Перевірка створеного backup не пройдена: {check[0] if check else 'немає відповіді'}")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


class Command(BaseCommand):
    help = "Перевірити або безпечно нормалізувати стару базу AutoЗапас до одного локального магазину."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Застосувати зміни (без цього режим лише читає базу).")
        parser.add_argument("--backup", help="Шлях до нового SQLite backup-файлу; обов'язковий разом із --apply.")
        parser.add_argument("--database", default="default", help="Alias бази Django (default: default).")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Вивести звіт у JSON.")

    def handle(self, *args, **options):
        alias = options["database"]
        if alias not in connections:
            raise CommandError(f"Невідома база даних: {alias}")
        if options["as_json"] and options["apply"]:
            raise CommandError("--json призначений для read-only аудиту; не поєднуйте його з --apply.")
        if options["apply"] and not options["backup"]:
            raise CommandError("Для --apply обов'язково вкажіть --backup /шлях/до/backup.sqlite3.")
        if not options["apply"] and options["backup"]:
            raise CommandError("--backup використовується лише разом із --apply.")

        Company = apps.get_model("core", "Company")
        Membership = apps.get_model("core", "CompanyMembership")
        User = apps.get_model(settings.AUTH_USER_MODEL)
        stores = list(Company.objects.using(alias).order_by("pk"))
        scoped = company_scoped_models(Company)

        counts = []
        total_rows = 0
        null_rows = 0
        for model, field in scoped:
            qs = model.objects.using(alias)
            count = qs.count()
            null_count = qs.filter(**{f"{field.name}__isnull": True}).count()
            total_rows += count
            null_rows += null_count
            counts.append({
                "model": model._meta.label,
                "rows": count,
                "without_store": null_count,
            })

        memberships = list(
            Membership.objects.using(alias).select_related("user", "company").order_by("company_id", "user_id")
        )
        active_users_without_role = []
        if len(stores) == 1:
            member_ids = set(
                Membership.objects.using(alias).filter(company_id=stores[0].pk, is_active=True)
                .values_list("user_id", flat=True)
            )
            # A store owner can be safely attached as owner during apply.
            if stores[0].owner_id:
                member_ids.add(stores[0].owner_id)
            active_users_without_role = list(
                User.objects.using(alias).filter(is_active=True, is_superuser=False)
                .exclude(pk__in=member_ids).values_list("username", flat=True)
            )

        cross_mismatches = []
        unique_collisions = []
        if len(stores) == 1:
            for model, field in scoped:
                mismatch_count = cross_company_reference_mismatches(model, field, alias, Company)
                if mismatch_count:
                    cross_mismatches.append({"model": model._meta.label, "rows": mismatch_count})
                for collision in company_unique_key_collisions(model, field, stores[0].pk, alias):
                    unique_collisions.append({"model": model._meta.label, **collision})

        blockers = []
        if len(stores) > 1:
            blockers.append(
                "Знайдено кілька магазинів. Автоматичне злиття заборонене: спочатку потрібна окрема таблиця відповідності "
                "дублікатів і перевірка конфліктів товарів, клієнтів, шаблонів та фінансових документів."
            )
        if cross_mismatches:
            blockers.append("Є записи, пов'язані з об'єктами іншого магазину.")
        if unique_collisions:
            blockers.append("Після заповнення company_id виникнуть конфлікти унікальності (наприклад, дублікати телефонів, штрихкодів або шаблонів).")
        if active_users_without_role:
            blockers.append("Є активні облікові записи без ролі в локальному магазині.")

        owner_report = None
        if len(stores) == 1:
            store = stores[0]
            owner_memberships = list(
                Membership.objects.using(alias).filter(company_id=store.pk, role="owner", is_active=True)
                .values_list("user_id", flat=True)
            )
            owner_report = {
                "company_owner_id": store.owner_id,
                "active_owner_membership_user_ids": owner_memberships,
            }
            if store.owner_id:
                if owner_memberships and owner_memberships != [store.owner_id]:
                    blockers.append("У магазині є інша активна роль owner, ніж власник у Company.")
                owner_membership = Membership.objects.using(alias).filter(
                    company_id=store.pk, user_id=store.owner_id
                ).first()
                if owner_membership and (owner_membership.role != "owner" or not owner_membership.is_active):
                    blockers.append("Обліковий запис власника має несумісну або неактивну роль працівника.")
                owner_user = User.objects.using(alias).filter(pk=store.owner_id).first()
                if not owner_user or not owner_user.is_active:
                    blockers.append("Власник магазину відсутній або неактивний.")
            elif len(owner_memberships) != 1:
                blockers.append("Неможливо однозначно визначити власника локального магазину.")

        report = {
            "database": alias,
            "stores": [
                {"id": s.pk, "name": s.name, "slug": s.slug, "owner_id": s.owner_id}
                for s in stores
            ],
            "store_count": len(stores),
            "business_models": counts,
            "business_rows": total_rows,
            "rows_without_store": null_rows,
            "membership_count": len(memberships),
            "active_users_without_role": active_users_without_role,
            "cross_store_reference_mismatches": cross_mismatches,
            "unique_key_collisions": unique_collisions,
            "owner_consistency": owner_report,
            "blockers": blockers,
            "mode": "apply" if options["apply"] else "read_only",
        }

        if options["as_json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("Аудит локальної бази AutoЗапас"))
            self.stdout.write(f"Магазинів: {len(stores)}")
            for store in stores:
                self.stdout.write(f"  #{store.pk}: {store.name} | slug={store.slug} | owner_id={store.owner_id}")
            self.stdout.write(f"Операційних моделей: {len(counts)}; записів: {total_rows}; без магазину: {null_rows}")
            for row in counts:
                if row["rows"] or row["without_store"]:
                    self.stdout.write(f"  {row['model']}: {row['rows']} записів, без магазину: {row['without_store']}")
            self.stdout.write(f"Ролей працівників: {len(memberships)}")
            if active_users_without_role:
                self.stdout.write("Активні користувачі без ролі: " + ", ".join(active_users_without_role))
            if cross_mismatches:
                self.stdout.write("Порушення зв'язків між магазинами: " + ", ".join(
                    f"{x['model']}={x['rows']}" for x in cross_mismatches
                ))
            if unique_collisions:
                self.stdout.write("Конфлікти унікальності: " + ", ".join(
                    f"{x['model']}:{x['constraint']} key={x['key']}" for x in unique_collisions[:10]
                ))
            if blockers:
                self.stdout.write(self.style.ERROR("Перенесення заблоковано:"))
                for item in blockers:
                    self.stdout.write(f"  - {item}")
            else:
                self.stdout.write(self.style.SUCCESS("Критичних конфліктів не виявлено."))

        if blockers:
            if options["apply"]:
                raise CommandError("Зміни не застосовано. Спочатку усуньте блокери зі звіту.")
            return

        if not options["apply"]:
            if not options["as_json"]:
                self.stdout.write("Режим перевірки: база не змінювалася. Для застосування потрібні --apply та --backup.")
            return

        if connections[alias].vendor != "sqlite":
            raise CommandError("Застосування перенесення наразі дозволене лише для SQLite; PostgreSQL буде окремим етапом.")

        backup_path = create_sqlite_backup(alias, options["backup"])
        with transaction.atomic(using=alias):
            if len(stores) == 0:
                owner = User.objects.using(alias).filter(is_active=True, is_superuser=False).order_by("pk").first()
                if owner is None:
                    owner = User.objects.using(alias).filter(is_active=True).order_by("pk").first()
                if owner is None:
                    raise CommandError("Немає активного користувача. Спочатку створіть адміністратора через /setup/.")
                base_slug = "local-autozapaz"
                slug = base_slug
                suffix = 2
                while Company.objects.using(alias).filter(slug=slug).exists():
                    slug = f"{base_slug}-{suffix}"
                    suffix += 1
                store = Company.objects.using(alias).create(
                    name="АвтоЗапас", slug=slug, owner=owner, plan="start", status="active"
                )
                Membership.objects.using(alias).create(
                    company=store, user=owner, role="owner", is_active=True
                )
            else:
                store = Company.objects.using(alias).get(pk=stores[0].pk)

            updated = {}
            for model, field in scoped:
                changed = model.objects.using(alias).filter(**{f"{field.name}__isnull": True}).update(
                    **{field.attname: store.pk}
                )
                if changed:
                    updated[model._meta.label] = changed

            if store.owner_id:
                Membership.objects.using(alias).get_or_create(
                    company_id=store.pk, user_id=store.owner_id,
                    defaults={"role": "owner", "is_active": True},
                )
            else:
                owner_id = list(
                    Membership.objects.using(alias).filter(
                        company_id=store.pk, role="owner", is_active=True
                    ).values_list("user_id", flat=True)
                )[0]
                store.owner_id = owner_id
                store.save(update_fields=["owner"])

        self.stdout.write(self.style.SUCCESS(f"Локальний магазин нормалізовано. Backup: {backup_path}"))
        self.stdout.write(json.dumps({"store_id": store.pk, "updated_rows": updated}, ensure_ascii=False, indent=2))
