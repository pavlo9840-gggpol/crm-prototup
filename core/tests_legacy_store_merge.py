from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase

from core.models import (
    Category, Company, CompanyMembership, CompanySettings, Customer, Product,
    CustomerCar, Sale, SaleItem, StockMovement,
)


class LegacyStoreMergeCommandTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="merge-owner", password="secret")
        self.target = Company.objects.order_by("pk").first()
        self.target.name = "Цільовий магазин"
        self.target.slug = "merge-target"
        self.target.owner = self.owner
        self.target.status = "active"
        self.target.save(update_fields=["name", "slug", "owner", "status"])
        CompanyMembership.objects.create(
            company=self.target, user=self.owner, role="owner", is_active=True
        )
        self.source = Company.objects.create(
            name="Старий магазин", slug="merge-source", owner=self.owner, status="active"
        )
        CompanyMembership.objects.create(
            company=self.source, user=self.owner, role="owner", is_active=True
        )

    def run_merge(self, **kwargs):
        output = StringIO()
        call_command(
            "merge_legacy_stores",
            target_store=self.target.pk,
            source_stores=str(self.source.pk),
            stdout=output,
            **kwargs,
        )
        return output.getvalue()

    def test_read_only_report_contains_explicit_store_mapping(self):
        product = Product.objects.create(
            company=self.source, name="Фільтр", sku="FILTER-MERGE"
        )
        output = self.run_merge(as_json=True)
        report = json.loads(output)

        product.refresh_from_db()
        self.assertEqual(product.company_id, self.source.pk)
        self.assertEqual(report["store_mapping"], [
            {"from_store_id": self.source.pk, "to_store_id": self.target.pk}
        ])
        self.assertEqual(report["blockers"], [])

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_apply_moves_business_rows_and_unassigned_rows_without_deleting_them(self, backup):
        backup.return_value = Path("/tmp/autozapaz-merge-backup.sqlite3")
        category = Category.objects.create(company=self.source, name="Категорія старого магазину")
        source_product = Product.objects.create(
            company=self.source, category=category, name="Фільтр", sku="FILTER-MOVE"
        )
        unassigned_product = Product.objects.create(
            company=None, name="Старий товар без магазину", sku="FILTER-NULL"
        )

        output = self.run_merge(
            apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3"
        )

        backup.assert_called_once()
        source_product.refresh_from_db()
        unassigned_product.refresh_from_db()
        category.refresh_from_db()
        self.assertEqual(source_product.company_id, self.target.pk)
        self.assertEqual(unassigned_product.company_id, self.target.pk)
        self.assertEqual(category.company_id, self.target.pk)
        self.assertTrue(Company.objects.filter(pk=self.target.pk).exists())
        self.assertFalse(Company.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(
            CompanyMembership.objects.filter(company=self.target, user=self.owner).count(), 1
        )
        self.assertIn("Об'єднання завершено", output)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_duplicate_customer_phone_blocks_apply_without_backup(self, backup):
        Customer.objects.create(company=self.target, name="Клієнт А", phone="+380501112233")
        Customer.objects.create(company=self.source, name="Клієнт Б", phone="+380501112233")

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)
        self.assertEqual(Customer.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_unassigned_customer_that_would_conflict_is_reported(self, backup):
        Customer.objects.create(company=self.target, name="Клієнт А", phone="+380501112244")
        Customer.objects.create(company=None, name="Старий клієнт", phone="+380501112244")

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Customer.objects.filter(company__isnull=True).count(), 1)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_company_settings_collision_blocks_merge(self, backup):
        CompanySettings.objects.create(company=self.target, name="Налаштування А")
        CompanySettings.objects.create(company=self.source, name="Налаштування Б")

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(CompanySettings.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_second_active_owner_blocks_merge(self, backup):
        other = get_user_model().objects.create_user(username="merge-extra-owner", password="secret")
        CompanyMembership.objects.create(
            company=self.source, user=other, role="owner", is_active=True
        )

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_duplicate_product_barcode_blocks_merge(self, backup):
        Product.objects.create(
            company=self.target, name="Товар А", sku="PART-A", barcode="482000001"
        )
        Product.objects.create(
            company=self.source, name="Товар Б", sku="PART-B", barcode="482000001"
        )

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Product.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_conflicting_staff_roles_block_merge(self, backup):
        CompanyMembership.objects.filter(company=self.target, user=self.owner).update(role="manager")
        CompanyMembership.objects.filter(company=self.source, user=self.owner).update(role="cashier")

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_different_source_owner_blocks_merge(self, backup):
        other = get_user_model().objects.create_user(username="merge-other-owner", password="secret")
        self.source.owner = other
        self.source.save(update_fields=["owner"])

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_unselected_company_blocks_merge(self, backup):
        other = Company.objects.create(name="Третій", slug="merge-third", owner=self.owner)

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertTrue(Company.objects.filter(pk=other.pk).exists())

    def test_json_report_is_machine_readable(self):
        report = json.loads(self.run_merge(as_json=True))
        self.assertEqual(report["target_store"]["id"], self.target.pk)
        self.assertEqual(report["source_stores"][0]["id"], self.source.pk)
        self.assertEqual(report["blockers"], [])

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_duplicate_product_brand_and_normalized_sku_blocks_merge(self, backup):
        Product.objects.create(
            company=self.target, name="Фільтр оливи", brand="Bosch", sku="1 234-AB"
        )
        Product.objects.create(
            company=self.source, name="Масляний фільтр", brand=" BOSCH ", sku="1234AB"
        )

        report = json.loads(self.run_merge(as_json=True))
        self.assertEqual(len(report["business_duplicate_candidates"]), 1)
        self.assertEqual(report["business_duplicate_candidates"][0]["kind"], "product_brand_sku")
        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_customer_phone_duplicate_ignores_formatting_and_blocks_merge(self, backup):
        Customer.objects.create(
            company=self.target, name="Клієнт А", phone="+38 (050) 111-22-33"
        )
        Customer.objects.create(
            company=self.source, name="Клієнт Б", phone="380501112233"
        )

        report = json.loads(self.run_merge(as_json=True))
        self.assertEqual(len(report["business_duplicate_candidates"]), 1)
        self.assertEqual(report["business_duplicate_candidates"][0]["kind"], "customer_phone")
        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Customer.objects.count(), 2)

    def _write_record_map(self, payload):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "record-map.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_manual_product_merge_redirects_sales_and_stock_history(self, backup):
        backup.return_value = Path("/tmp/autozapaz-merge-backup.sqlite3")
        keep = Product.objects.create(
            company=self.target, name="Фільтр Bosch", brand="Bosch",
            sku="1 234-AB", current_stock=2
        )
        duplicate = Product.objects.create(
            company=self.source, name="Фільтр оливи", brand=" BOSCH ",
            sku="1234AB", current_stock=7
        )
        sale = Sale.objects.create(company=self.source, cashier=self.owner, total=100)
        sale_item = SaleItem.objects.create(
            company=self.source, sale=sale, product=duplicate,
            quantity=1, original_price=100, price=100
        )
        movement = StockMovement.objects.create(
            company=self.source, product=duplicate, movement_type="purchase",
            quantity_change=7, balance_after=7
        )
        mapping = self._write_record_map({
            "products": [{
                "keep_id": keep.pk, "merge_ids": [duplicate.pk], "current_stock": "9.000"
            }]
        })

        self.run_merge(
            apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3", record_map=mapping
        )

        backup.assert_called_once()
        keep.refresh_from_db()
        sale_item.refresh_from_db()
        movement.refresh_from_db()
        self.assertEqual(keep.current_stock, 9)
        self.assertEqual(sale_item.product_id, keep.pk)
        self.assertEqual(movement.product_id, keep.pk)
        self.assertFalse(Product.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_manual_customer_merge_redirects_sales_and_cars(self, backup):
        backup.return_value = Path("/tmp/autozapaz-merge-backup.sqlite3")
        keep = Customer.objects.create(
            company=self.target, name="Клієнт основний", phone="+380501112233"
        )
        duplicate = Customer.objects.create(
            company=self.source, name="Клієнт дубль", phone="380501112233"
        )
        sale = Sale.objects.create(
            company=self.source, cashier=self.owner, customer=duplicate, total=250
        )
        car = CustomerCar.objects.create(
            company=self.source, customer=duplicate, plate="AC1234AA", vin="WVWZZZ1JZXW000001"
        )
        mapping = self._write_record_map({
            "customers": [{
                "keep_id": keep.pk, "merge_ids": [duplicate.pk],
                "total_spent": "250.00", "purchases_count": 1
            }]
        })

        self.run_merge(
            apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3", record_map=mapping
        )

        backup.assert_called_once()
        keep.refresh_from_db()
        sale.refresh_from_db()
        car.refresh_from_db()
        self.assertEqual(keep.total_spent, 250)
        self.assertEqual(keep.purchases_count, 1)
        self.assertEqual(sale.customer_id, keep.pk)
        self.assertEqual(car.customer_id, keep.pk)
        self.assertFalse(Customer.objects.filter(pk=duplicate.pk).exists())

    def test_record_map_requires_explicit_product_stock_and_customer_totals(self):
        keep = Product.objects.create(
            company=self.target, name="Фільтр Bosch", brand="Bosch", sku="ABC-1"
        )
        duplicate = Product.objects.create(
            company=self.source, name="Фільтр Bosch", brand="Bosch", sku="ABC1"
        )
        mapping = self._write_record_map({
            "products": [{"keep_id": keep.pk, "merge_ids": [duplicate.pk]}]
        })
        with self.assertRaises(CommandError):
            self.run_merge(as_json=True, record_map=mapping)
        self.assertTrue(Product.objects.filter(pk=duplicate.pk).exists())

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_active_staff_without_membership_blocks_merge(self, backup):
        unassigned = get_user_model().objects.create_user(
            username="merge-unassigned-staff", password="secret"
        )

        report = json.loads(self.run_merge(as_json=True))
        self.assertEqual(report["active_users_without_role"], [unassigned.pk])
        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)

    @patch("core.management.commands.merge_legacy_stores.create_sqlite_backup")
    def test_multiple_owner_roles_block_when_target_owner_is_missing(self, backup):
        self.target.owner = None
        self.target.save(update_fields=["owner"])
        other = get_user_model().objects.create_user(
            username="merge-second-owner-no-target", password="secret"
        )
        CompanyMembership.objects.create(
            company=self.source, user=other, role="owner", is_active=True
        )

        with self.assertRaises(CommandError):
            self.run_merge(apply=True, backup="/tmp/autozapaz-merge-backup.sqlite3")

        backup.assert_not_called()
        self.assertEqual(Company.objects.count(), 2)
