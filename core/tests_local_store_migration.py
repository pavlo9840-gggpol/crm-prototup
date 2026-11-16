from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase

from core.models import Category, Company, CompanyMembership, CompanySettings, Product
from django.contrib.auth import get_user_model


class LocalStoreMigrationCommandTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="migration-owner", password="secret")
        # Migration 0026 creates the legacy local-store row before test users
        # exist. Reuse it so these tests start from one realistic old install.
        self.store = Company.objects.order_by("pk").first()
        self.store.name = "Основний магазин"
        self.store.owner = self.owner
        self.store.status = "active"
        self.store.save(update_fields=["name", "owner", "status"])
        CompanyMembership.objects.create(company=self.store, user=self.owner, role="owner", is_active=True)

    def run_audit(self, **kwargs):
        output = StringIO()
        call_command("migrate_to_local_store", stdout=output, **kwargs)
        return output.getvalue()

    def test_default_mode_is_read_only_and_reports_unassigned_rows(self):
        product = Product.objects.create(name="Фільтр", sku="FILTER-1", company=None)
        output = self.run_audit()

        product.refresh_from_db()
        self.assertIsNone(product.company_id)
        self.assertIn("без магазину: 1", output)
        self.assertIn("Режим перевірки", output)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_apply_assigns_null_company_rows_after_backup(self, backup):
        backup.return_value = Path("/tmp/autozapaz-test-backup.sqlite3")
        product = Product.objects.create(name="Фільтр", sku="FILTER-2", company=None)

        self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_called_once()
        product.refresh_from_db()
        self.assertEqual(product.company_id, self.store.pk)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_multiple_stores_are_rejected_without_mutating_data(self, backup):
        other_owner = get_user_model().objects.create_user(username="second-owner", password="secret")
        second = Company.objects.create(
            name="Другий магазин", slug="migration-second", owner=other_owner, status="active"
        )
        CompanyMembership.objects.create(company=second, user=other_owner, role="owner", is_active=True)
        product = Product.objects.create(name="Деталь", sku="SECOND-1", company=second)

        with self.assertRaises(CommandError):
            self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_not_called()
        product.refresh_from_db()
        self.assertEqual(product.company_id, second.pk)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_cross_store_foreign_key_mismatch_blocks_apply(self, backup):
        other_owner = get_user_model().objects.create_user(username="category-owner", password="secret")
        second = Company.objects.create(
            name="Інший магазин", slug="migration-category", owner=other_owner, status="active"
        )
        category = Category.objects.create(company=second, name="Чужа категорія")
        product = Product.objects.create(
            company=self.store, category=category, name="Деталь", sku="CROSS-1"
        )

        with self.assertRaises(CommandError):
            self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_not_called()
        product.refresh_from_db()
        self.assertEqual(product.category_id, category.pk)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_company_settings_unique_collision_blocks_apply(self, backup):
        CompanySettings.objects.create(company=self.store, name="Налаштування магазину")
        CompanySettings.objects.create(company=None, name="Старі налаштування")

        with self.assertRaises(CommandError):
            self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(CompanySettings.objects.filter(company__isnull=True).count(), 1)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_owner_is_recovered_from_one_unambiguous_owner_role(self, backup):
        backup.return_value = Path("/tmp/autozapaz-test-backup.sqlite3")
        self.store.owner = None
        self.store.save(update_fields=["owner"])

        self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        self.store.refresh_from_db()
        self.assertEqual(self.store.owner_id, self.owner.pk)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_active_staff_without_role_blocks_apply(self, backup):
        get_user_model().objects.create_user(username="unassigned-staff", password="secret")

        with self.assertRaises(CommandError):
            self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_not_called()

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_first_local_store_is_created_for_one_unambiguous_active_user(self, backup):
        backup.return_value = Path("/tmp/autozapaz-test-backup.sqlite3")
        Category.objects.filter(company=self.store).update(company=None)
        self.store.delete()
        product = Product.objects.create(name="Підшипник", sku="BEARING-1", company=None)

        self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        created = Company.objects.get()
        self.assertEqual(created.owner_id, self.owner.pk)
        self.assertTrue(CompanyMembership.objects.filter(
            company=created, user=self.owner, role="owner", is_active=True
        ).exists())
        product.refresh_from_db()
        self.assertEqual(product.company_id, created.pk)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_first_local_store_refuses_to_guess_roles_for_multiple_users(self, backup):
        Category.objects.filter(company=self.store).update(company=None)
        self.store.delete()
        get_user_model().objects.create_user(username="second-unassigned", password="secret")

        with self.assertRaises(CommandError):
            self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 0)

    @patch("core.management.commands.migrate_to_local_store.create_sqlite_backup")
    def test_duplicate_unassigned_unique_keys_block_first_store_creation(self, backup):
        Category.objects.filter(company=self.store).update(company=None)
        self.store.delete()
        CompanySettings.objects.create(company=None, name="Старі налаштування А")
        CompanySettings.objects.create(company=None, name="Старі налаштування Б")

        with self.assertRaises(CommandError):
            self.run_audit(apply=True, backup="/tmp/autozapaz-test-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 0)

    def test_json_report_is_machine_readable(self):
        import json
        output = StringIO()
        call_command("migrate_to_local_store", as_json=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["store_count"], 1)
        self.assertEqual(report["blockers"], [])
