"""Safely consolidate multiple legacy stores into one local AutoZapaz store.

The command is read-only by default. It refuses to merge when unique keys,
cross-store relations, or staff roles are ambiguous. It never merges or deletes
business records; only company ownership is consolidated after preflight.
"""
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.models import Q
from django.db.models.fields.related import ForeignKey

from .migrate_to_local_store import company_fk, company_scoped_models, create_sqlite_backup


def parse_ids(value):
    try:
        ids = [int(part.strip()) for part in value.split(",") if part.strip()]
    except (TypeError, ValueError):
        raise CommandError("--source-stores має містити ID через кому, наприклад: 2,3.")
    if not ids or len(ids) != len(set(ids)):
        raise CommandError("--source-stores має містити хоча б один унікальний ID.")
    return ids


def _condition_without_company_null_checks(condition, company_field):
    """Evaluate partial unique conditions as if NULL-owned rows were assigned to a store."""
    if condition is None:
        return None
    result = Q()
    result.connector = condition.connector
    result.negated = condition.negated
    for child in condition.children:
        if isinstance(child, Q):
            result.children.append(_condition_without_company_null_checks(child, company_field))
        else:
            lookup, _value = child
            root = lookup.split("__", 1)[0]
            if root in {company_field.name, company_field.attname}:
                continue
            result.children.append(child)
    return result


def unique_company_collisions(model, company_field, company_ids, alias):
    """Find exact unique-key collisions, including unassigned rows that will move."""
    candidates = []
    seen = set()
    for constraint in model._meta.constraints:
        fields = getattr(constraint, "fields", None)
        if fields and company_field.name in fields:
            candidates.append((constraint.name, [
                name for name in fields if name != company_field.name
            ], getattr(constraint, "condition", None)))
            seen.add(tuple(fields))
    for unique_group in model._meta.unique_together:
        if company_field.name in unique_group and tuple(unique_group) not in seen:
            candidates.append(("unique_together", [
                name for name in unique_group if name != company_field.name
            ], None))

    collisions = []
    base = model.objects.using(alias).filter(
        Q(**{f"{company_field.attname}__in": company_ids})
        | Q(**{f"{company_field.attname}__isnull": True})
    )
    for constraint_name, key_names, condition in candidates:
        key_attnames = [model._meta.get_field(name).attname for name in key_names]
        if condition is None:
            qs = base
        else:
            adjusted = _condition_without_company_null_checks(condition, company_field)
            # Existing rows obey the real partial condition; NULL-owned rows
            # are evaluated with company predicates removed because they will
            # receive the target company before the unique index is checked.
            qs = (
                base.filter(**{f"{company_field.attname}__isnull": False}).filter(condition)
                | base.filter(**{f"{company_field.attname}__isnull": True}).filter(adjusted)
            )
        grouped = defaultdict(list)
        selected = ["pk", company_field.attname, *key_attnames]
        for row in qs.values_list(*selected).iterator():
            pk, company_id, *values = row
            if any(value is None for value in values):
                continue
            grouped[tuple(values)].append({"id": pk, "company_id": company_id})

        for key, records in grouped.items():
            company_counts = defaultdict(int)
            for record in records:
                company_counts[record["company_id"]] += 1
            if len(company_counts) > 1 or company_counts.get(None, 0) > 1:
                collisions.append({
                    "constraint": constraint_name,
                    "key_fields": key_names,
                    "key": list(key),
                    "records": records,
                })
    return collisions


def semantic_duplicate_candidates(company_ids, alias):
    """Find likely duplicate products/customers across stores before ownership consolidation."""
    Product = apps.get_model("core", "Product")
    Customer = apps.get_model("core", "Customer")
    scope = Q(company_id__in=company_ids) | Q(company_id__isnull=True)
    candidates = []

    products = Product.objects.using(alias).filter(scope).values(
        "pk", "company_id", "name", "brand", "sku", "sku_norm", "barcode"
    )
    grouped_products = defaultdict(list)
    for row in products.iterator():
        brand_key = " ".join((row["brand"] or "").split()).casefold()
        sku_key = (row["sku_norm"] or "").strip().upper()
        if brand_key and sku_key:
            grouped_products[(brand_key, sku_key)].append(row)
    for key, rows in grouped_products.items():
        store_ids = {row["company_id"] for row in rows}
        if len(store_ids) > 1:
            candidates.append({
                "kind": "product_brand_sku",
                "key": {"brand": key[0], "sku_norm": key[1]},
                "records": [
                    {"id": row["pk"], "company_id": row["company_id"],
                     "name": row["name"], "brand": row["brand"],
                     "sku": row["sku"], "barcode": row["barcode"]}
                    for row in rows
                ],
            })

    customers = Customer.objects.using(alias).filter(scope).values(
        "pk", "company_id", "name", "phone"
    )
    grouped_customers = defaultdict(list)
    for row in customers.iterator():
        phone_key = "".join(ch for ch in (row["phone"] or "") if ch.isdigit())
        if len(phone_key) >= 7:
            grouped_customers[phone_key].append(row)
    for phone_key, rows in grouped_customers.items():
        store_ids = {row["company_id"] for row in rows}
        if len(store_ids) > 1:
            candidates.append({
                "kind": "customer_phone",
                "key": {"phone_digits": phone_key},
                "records": [
                    {"id": row["pk"], "company_id": row["company_id"],
                     "name": row["name"], "phone": row["phone"]}
                    for row in rows
                ],
            })
    return candidates



def load_record_merge_plan(path, selected_company_ids, alias):
    """Load explicit product/customer merges; never infer financial or stock totals."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Не вдалося прочитати --record-map: {exc}")
    if not isinstance(payload, dict) or set(payload) - {"products", "customers"}:
        raise CommandError("--record-map має містити лише масиви products та customers.")

    plan = {"core.Product": [], "core.Customer": []}
    used_ids = {"core.Product": set(), "core.Customer": set()}
    for key, label in (("products", "core.Product"), ("customers", "core.Customer")):
        entries = payload.get(key, [])
        if not isinstance(entries, list):
            raise CommandError(f"Поле {key} у --record-map має бути масивом.")
        Model = apps.get_model(*label.split("."))
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise CommandError(f"{key}[{index}] має бути об'єктом.")
            try:
                keep_id = int(entry["keep_id"])
                merge_ids = [int(value) for value in entry["merge_ids"]]
            except (KeyError, TypeError, ValueError):
                raise CommandError(f"{key}[{index}]: потрібні числові keep_id та merge_ids.")
            if not merge_ids or keep_id in merge_ids or len(merge_ids) != len(set(merge_ids)):
                raise CommandError(f"{key}[{index}]: merge_ids мають бути непорожніми й унікальними.")
            all_ids = [keep_id, *merge_ids]
            if used_ids[label].intersection(all_ids):
                raise CommandError(f"{key}[{index}]: один запис використано у кількох групах.")
            rows = list(Model.objects.using(alias).filter(pk__in=all_ids).values("pk", "company_id"))
            by_id = {row["pk"]: row for row in rows}
            if set(by_id) != set(all_ids):
                raise CommandError(f"{key}[{index}]: один або кілька ID не знайдені.")
            if any(row["company_id"] not in selected_company_ids and row["company_id"] is not None
                   for row in rows):
                raise CommandError(f"{key}[{index}]: записи мають належати вибраним магазинам або бути без магазину.")
            if len({by_id[value]["company_id"] for value in all_ids}) < 2:
                raise CommandError(f"{key}[{index}]: група має охоплювати щонайменше два магазини/джерела.")
            normalized = {"keep_id": keep_id, "merge_ids": merge_ids}
            if label == "core.Product":
                if "current_stock" not in entry:
                    raise CommandError(f"{key}[{index}]: явно вкажіть current_stock для основної картки.")
                try:
                    stock = Decimal(str(entry["current_stock"]))
                    if not stock.is_finite():
                        raise InvalidOperation
                except (InvalidOperation, TypeError, ValueError):
                    raise CommandError(f"{key}[{index}]: current_stock має бути скінченним числом.")
                normalized["current_stock"] = stock
            else:
                if "total_spent" not in entry or "purchases_count" not in entry:
                    raise CommandError(f"{key}[{index}]: явно вкажіть total_spent та purchases_count.")
                try:
                    spent = Decimal(str(entry["total_spent"]))
                    count = int(entry["purchases_count"])
                    if not spent.is_finite() or count < 0:
                        raise InvalidOperation
                except (InvalidOperation, TypeError, ValueError):
                    raise CommandError(f"{key}[{index}]: некоректні total_spent або purchases_count.")
                normalized.update(total_spent=spent, purchases_count=count)
            plan[label].append(normalized)
            used_ids[label].update(all_ids)
    return plan


def record_merge_coverage(plan):
    return {
        label: {
            record_id
            for group in groups
            for record_id in [group["keep_id"], *group["merge_ids"]]
        }
        for label, groups in plan.items()
    }


def redirect_record_foreign_keys(Model, keep_id, merge_ids, alias):
    """Redirect every FK/OneToOne reference before deleting a duplicate record."""
    changed = {}
    for related_model in apps.get_models():
        for field in related_model._meta.fields:
            if isinstance(field, ForeignKey) and field.remote_field.model == Model:
                count = related_model.objects.using(alias).filter(
                    **{field.attname + "__in": merge_ids}
                ).update(**{field.attname: keep_id})
                if count:
                    key = related_model._meta.label
                    changed[key] = changed.get(key, 0) + count
    Model.objects.using(alias).filter(pk__in=merge_ids).delete()
    return changed


def apply_record_merge_plan(plan, alias):
    moved = {}
    Product = apps.get_model("core", "Product")
    Customer = apps.get_model("core", "Customer")
    for group in plan["core.Product"]:
        counts = redirect_record_foreign_keys(Product, group["keep_id"], group["merge_ids"], alias)
        Product.objects.using(alias).filter(pk=group["keep_id"]).update(current_stock=group["current_stock"])
        for model, count in counts.items():
            moved[model] = moved.get(model, 0) + count
    for group in plan["core.Customer"]:
        counts = redirect_record_foreign_keys(Customer, group["keep_id"], group["merge_ids"], alias)
        Customer.objects.using(alias).filter(pk=group["keep_id"]).update(
            total_spent=group["total_spent"], purchases_count=group["purchases_count"]
        )
        for model, count in counts.items():
            moved[model] = moved.get(model, 0) + count
    return moved


def external_reference_mismatches(model, source_company_field, company_ids, alias, company_model):
    """Rows in selected stores pointing to related objects in a store outside the merge."""
    qs = model.objects.using(alias).filter(
        Q(**{f"{source_company_field.attname}__in": company_ids})
        | Q(**{f"{source_company_field.attname}__isnull": True})
    )
    mismatches = []
    for relation in model._meta.fields:
        if not isinstance(relation, ForeignKey) or relation.remote_field.model == company_model:
            continue
        target_company_field = company_fk(relation.remote_field.model, company_model)
        if not target_company_field:
            continue
        related_company = f"{relation.name}__{target_company_field.attname}"
        count = qs.filter(
            **{
                f"{relation.name}__isnull": False,
                f"{related_company}__isnull": False,
            }
        ).exclude(**{f"{related_company}__in": company_ids}).count()
        if count:
            mismatches.append({
                "model": model._meta.label,
                "relation": relation.name,
                "rows": count,
            })
    return mismatches


def staff_merge_plan(memberships, target_id, target_owner_id, source_companies):
    blockers = []
    by_user = defaultdict(list)
    for membership in memberships:
        by_user[membership.user_id].append(membership)

    plan = []
    for user_id, rows in by_user.items():
        roles = {(row.role, row.is_active) for row in rows}
        if len(roles) > 1:
            blockers.append({
                "user_id": user_id,
                "memberships": [
                    {"company_id": row.company_id, "role": row.role, "is_active": row.is_active}
                    for row in rows
                ],
            })
            continue
        target_rows = [row for row in rows if row.company_id == target_id]
        source_rows = [row for row in rows if row.company_id != target_id]
        if not source_rows:
            continue
        if target_rows:
            plan.append({
                "user_id": user_id,
                "action": "keep_target_remove_source_duplicates",
                "source_membership_ids": [row.pk for row in source_rows],
            })
        else:
            keep = sorted(source_rows, key=lambda row: (row.created_at, row.pk))[0]
            plan.append({
                "user_id": user_id,
                "action": "move_membership_to_target",
                "keep_membership_id": keep.pk,
                "remove_membership_ids": [row.pk for row in source_rows if row.pk != keep.pk],
            })

    active_owner_ids = {
        row.user_id for row in memberships if row.role == "owner" and row.is_active
    }
    if target_owner_id:
        unexpected_owners = sorted(active_owner_ids - {target_owner_id})
        if unexpected_owners:
            blockers.append({
                "user_ids": unexpected_owners,
                "reason": "Після об'єднання залишилися б додаткові активні власники. Потрібно явно змінити їхні ролі.",
            })
        owner_membership = next((row for row in memberships
                                 if row.company_id == target_id and row.user_id == target_owner_id), None)
        if owner_membership and (owner_membership.role != "owner" or not owner_membership.is_active):
            blockers.append({
                "user_id": target_owner_id,
                "reason": "Власник цільового магазину має несумісну або неактивну роль.",
            })

    for company in source_companies:
        if company.owner_id and target_owner_id and company.owner_id != target_owner_id:
            blockers.append({
                "company_id": company.pk,
                "owner_id": company.owner_id,
                "reason": "Власник старого магазину відрізняється від власника цільового. Потрібне явне рішення щодо ролі.",
            })
    return plan, blockers


class Command(BaseCommand):
    help = "Перевірити або безпечно об'єднати старі магазини в один локальний магазин."

    def add_arguments(self, parser):
        parser.add_argument("--target-store", type=int, help="ID магазину, який залишиться.")
        parser.add_argument("--source-stores", help="ID усіх інших магазинів через кому.")
        parser.add_argument("--apply", action="store_true", help="Застосувати об'єднання після чистого аудиту.")
        parser.add_argument("--backup", help="Новий SQLite backup-файл поза каталогом проєкту.")
        parser.add_argument("--record-map", help="JSON із явним ручним зіставленням дублікатів товарів і клієнтів.")
        parser.add_argument("--database", default="default", help="Alias бази Django.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Вивести звіт у JSON.")

    def handle(self, *args, **options):
        alias = options["database"]
        if alias not in connections:
            raise CommandError(f"Невідома база даних: {alias}")
        if not options["target_store"] or not options["source_stores"]:
            raise CommandError("Вкажіть --target-store та --source-stores. Спочатку виконайте --help.")
        if options["apply"] and not options["backup"]:
            raise CommandError("Для --apply обов'язково потрібен --backup.")
        if not options["apply"] and options["backup"]:
            raise CommandError("--backup дозволений лише разом із --apply.")
        if options["as_json"] and options["apply"]:
            raise CommandError("--json призначений лише для read-only звіту.")

        target_id = options["target_store"]
        source_ids = parse_ids(options["source_stores"])
        if target_id in source_ids:
            raise CommandError("Цільовий магазин не може бути одночасно у списку джерел.")

        Company = apps.get_model("core", "Company")
        Membership = apps.get_model("core", "StaffMembership")
        all_companies = list(Company.objects.using(alias).order_by("pk"))
        by_id = {company.pk: company for company in all_companies}
        missing = [pk for pk in [target_id, *source_ids] if pk not in by_id]
        if missing:
            raise CommandError("Не знайдено магазини з ID: " + ", ".join(map(str, missing)))

        selected_ids = [target_id, *source_ids]
        unselected_ids = sorted(set(by_id) - set(selected_ids))
        blockers = []
        if unselected_ids:
            blockers.append(
                "Щоб отримати одну локальну базу, потрібно включити всі старі магазини. "
                f"Не включені ID: {unselected_ids}."
            )

        target = by_id[target_id]
        sources = [by_id[pk] for pk in source_ids]
        scoped = company_scoped_models(Company)
        counts = []
        total_rows = 0
        for model, field in scoped:
            per_store = {
                str(company_id): model.objects.using(alias).filter(
                    **{field.attname: company_id}
                ).count()
                for company_id in selected_ids
            }
            per_store["unassigned"] = model.objects.using(alias).filter(
                **{f"{field.attname}__isnull": True}
            ).count()
            count = sum(per_store.values())
            total_rows += count
            if count:
                counts.append({"model": model._meta.label, "rows_by_store": per_store, "total": count})

        collisions = []
        mismatches = []
        for model, field in scoped:
            collisions.extend({
                "model": model._meta.label,
                **item,
            } for item in unique_company_collisions(model, field, selected_ids, alias))
            mismatches.extend(
                external_reference_mismatches(model, field, selected_ids, alias, Company)
            )
        semantic_duplicates = semantic_duplicate_candidates(selected_ids, alias)
        record_plan = load_record_merge_plan(options["record_map"], selected_ids, alias) if options["record_map"] else {
            "core.Product": [], "core.Customer": []
        }
        mapped_ids = record_merge_coverage(record_plan)
        unresolved_collisions = []
        for collision in collisions:
            label = collision["model"]
            ids = {row["id"] for row in collision["records"]}
            if label not in mapped_ids or not ids.issubset(mapped_ids[label]):
                unresolved_collisions.append(collision)
        if unresolved_collisions:
            blockers.append("Залишилися конфлікти унікальності, які не покриває файл ручного зіставлення.")
        unresolved_semantic = []
        for candidate in semantic_duplicates:
            label = "core.Product" if candidate["kind"] == "product_brand_sku" else "core.Customer"
            ids = {row["id"] for row in candidate["records"]}
            if not ids.issubset(mapped_ids[label]):
                unresolved_semantic.append(candidate)
        if unresolved_semantic:
            blockers.append(
                "Є ймовірні дублікати товарів/клієнтів без повного ручного зіставлення. "
                "Підготуйте --record-map; автоматичного вибору основної картки немає."
            )
        if mismatches:
            blockers.append(
                "Знайдено посилання на об'єкти магазинів поза вибраним набором. "
                "Спочатку потрібно включити відповідні магазини або виправити зв'язки."
            )

        memberships = list(
            Membership.objects.using(alias).filter(company_id__in=selected_ids)
            .select_related("user").order_by("user_id", "company_id", "pk")
        )
        User = apps.get_model(settings.AUTH_USER_MODEL)
        active_users_without_role = list(
            User.objects.using(alias).filter(is_active=True, is_superuser=False)
            .exclude(pk__in=Membership.objects.using(alias)
                     .filter(company_id__in=selected_ids).values("user_id"))
            .values_list("pk", flat=True)
        )
        if active_users_without_role:
            blockers.append(
                "Є активні облікові записи без ролі працівника: "
                f"{active_users_without_role}. Призначте їм ролі або деактивуйте облікові записи."
            )
        staff_plan, staff_conflicts = staff_merge_plan(
            memberships, target_id, target.owner_id, sources
        )
        if staff_conflicts:
            blockers.append("Знайдено конфлікти власників або ролей працівників. Потрібне явне рішення до об'єднання.")

        if not target.owner_id:
            owner_ids = {company.owner_id for company in [target, *sources] if company.owner_id}
            active_owner_roles = {row.user_id for row in memberships if row.role == "owner" and row.is_active}
            candidates = owner_ids | active_owner_roles
            if len(candidates) != 1:
                blockers.append("Неможливо однозначно визначити власника майбутнього локального магазину.")

        report = {
            "database": alias,
            "target_store": {
                "id": target.pk, "name": target.name, "slug": target.slug, "owner_id": target.owner_id,
            },
            "source_stores": [
                {"id": row.pk, "name": row.name, "slug": row.slug, "owner_id": row.owner_id}
                for row in sources
            ],
            "unselected_store_ids": unselected_ids,
            "store_mapping": [{"from_store_id": pk, "to_store_id": target_id} for pk in source_ids],
            "business_rows": total_rows,
            "business_models": counts,
            "unique_key_conflicts": collisions,
            "unresolved_unique_key_conflicts": unresolved_collisions,
            "business_duplicate_candidates": semantic_duplicates,
            "unresolved_business_duplicate_candidates": unresolved_semantic,
            "record_merge_plan": record_plan,
            "external_reference_mismatches": mismatches,
            "staff_merge_plan": staff_plan,
            "staff_conflicts": staff_conflicts,
            "active_users_without_role": active_users_without_role,
            "blockers": blockers,
            "mode": "apply" if options["apply"] else "read_only",
        }

        if options["as_json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("Аудит об'єднання магазинів АвтоЗапас"))
            self.stdout.write(f"Цільовий магазин: #{target.pk} — {target.name}")
            self.stdout.write("Таблиця відповідності: " + ", ".join(
                f"{pk} → {target_id}" for pk in source_ids
            ))
            self.stdout.write(f"Магазинів у базі: {len(all_companies)}; записів у вибраних магазинах: {total_rows}")
            for row in counts:
                self.stdout.write(f"  {row['model']}: {row['total']} записів")
            self.stdout.write(f"Конфліктів унікальності: {len(collisions)}")
            self.stdout.write(f"Ймовірних дублікатів товарів/клієнтів: {len(semantic_duplicates)}")
            self.stdout.write(f"Порушень зовнішніх зв'язків: {len(mismatches)}")
            self.stdout.write(f"Операцій з ролями працівників: {len(staff_plan)}")
            if blockers:
                self.stdout.write(self.style.ERROR("Об'єднання заблоковано:"))
                for item in blockers:
                    self.stdout.write(f"  - {item}")
            else:
                self.stdout.write(self.style.SUCCESS(
                    "Попередня перевірка пройдена. Бізнес-записи можна перенести без злиття дублікатів."
                ))

        if blockers:
            if options["apply"]:
                raise CommandError("Зміни не застосовано. Спочатку усуньте всі конфлікти.")
            return
        if not options["apply"]:
            if not options["as_json"]:
                self.stdout.write("Read-only: база не змінювалася. Для застосування потрібні --apply та --backup.")
            return
        if connections[alias].vendor != "sqlite":
            raise CommandError("Застосування наразі підтримує лише SQLite.")

        backup_path = create_sqlite_backup(alias, options["backup"])
        with transaction.atomic(using=alias):
            locked_companies = {
                row.pk: row for row in Company.objects.using(alias).select_for_update()
                .filter(pk__in=selected_ids)
            }
            if set(locked_companies) != set(selected_ids):
                raise CommandError("Склад магазинів змінився після аудиту. Запустіть команду повторно.")
            locked_target = locked_companies[target_id]

            merged_record_references = apply_record_merge_plan(record_plan, alias)
            moved_rows = {}
            for model, field in scoped:
                changed = model.objects.using(alias).filter(
                    Q(**{f"{field.attname}__in": source_ids})
                    | Q(**{f"{field.attname}__isnull": True})
                ).update(**{field.attname: target_id})
                if changed:
                    moved_rows[model._meta.label] = changed

            # Keep one membership per user in the target store. Role conflicts
            # were rejected during preflight; no role is silently promoted.
            current_memberships = list(
                Membership.objects.using(alias).filter(company_id__in=selected_ids)
                .order_by("user_id", "company_id", "pk")
            )
            grouped = defaultdict(list)
            for row in current_memberships:
                grouped[row.user_id].append(row)
            for user_id, rows in grouped.items():
                target_rows = [row for row in rows if row.company_id == target_id]
                source_rows = [row for row in rows if row.company_id != target_id]
                if target_rows:
                    Membership.objects.using(alias).filter(
                        pk__in=[row.pk for row in source_rows]
                    ).delete()
                elif source_rows:
                    keep = sorted(source_rows, key=lambda row: (row.created_at, row.pk))[0]
                    Membership.objects.using(alias).filter(pk=keep.pk).update(company_id=target_id)
                    Membership.objects.using(alias).filter(
                        pk__in=[row.pk for row in source_rows if row.pk != keep.pk]
                    ).delete()

            owner_id = locked_target.owner_id
            if not owner_id:
                candidates = {
                    row.owner_id for row in locked_companies.values() if row.owner_id
                } or {
                    row.user_id for row in current_memberships if row.role == "owner" and row.is_active
                }
                owner_id = next(iter(candidates))
                locked_target.owner_id = owner_id
                locked_target.save(update_fields=["owner", "updated_at"])
            Membership.objects.using(alias).get_or_create(
                company_id=target_id, user_id=owner_id,
                defaults={"role": "owner", "is_active": True},
            )
            Company.objects.using(alias).filter(pk__in=source_ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f"Об'єднання завершено. Збережено магазин #{target_id}; видалено магазинів: {len(source_ids)}; backup: {backup_path}"
        ))
        self.stdout.write(json.dumps({
            "target_store_id": target_id,
            "removed_store_ids": source_ids,
            "moved_rows_by_model": moved_rows,
            "redirected_record_references": merged_record_references,
        }, ensure_ascii=False, indent=2))
