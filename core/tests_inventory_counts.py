from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core import services
from core.models import (
    InventoryCount, InventoryCountItem, Product, ProductBarcode, StockAdjustment, StockMovement,
)
from core.services import StockError
from core.tests_tenant_helpers import create_test_company


D = Decimal


class InventoryCountWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("inventory-owner", "", "secret")
        self.company = create_test_company(self.user, "Inventory store")
        self.product = Product.objects.create(
            company=self.company, name="Кава", sku="COFFEE-1",
            barcode="4820000001000", current_stock=D("10"),
        )
        self.client.force_login(self.user)

    def create_count(self):
        response = self.client.post("/inventory/counts/new/", {"note": "Плановий перерахунок"})
        self.assertEqual(response.status_code, 302)
        return InventoryCount.objects.get(company=self.company)

    def test_new_document_snapshots_only_regular_stock_goods(self):
        Product.objects.create(
            company=self.company, name="Телефон", sku="PHONE-1", tracking_mode="serial",
        )
        Product.objects.create(
            company=self.company, name="Молоко", sku="MILK-1", tracking_mode="expiry",
        )
        Product.objects.create(
            company=self.company, name="Доставка", sku="DELIVERY", product_kind="service",
        )
        count = self.create_count()
        items = list(count.items.select_related("product"))
        self.assertEqual([item.product_id for item in items], [self.product.pk])
        self.assertEqual(items[0].expected_quantity, D("10"))
        self.assertIsNone(items[0].counted_quantity)

    def test_manual_count_approval_posts_shortage_to_stock_ledger(self):
        count = self.create_count()
        item = count.items.get(product=self.product)
        response = self.client.post(
            f"/inventory/counts/{count.pk}/",
            {f"counted_{item.pk}": "8", "action": "save"},
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.counted_quantity, D("8"))

        response = self.client.post(
            f"/inventory/counts/{count.pk}/", {"action": "approve"},
        )
        self.assertEqual(response.status_code, 302)
        count.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(count.status, InventoryCount.APPROVED)
        self.assertEqual(self.product.current_stock, D("8"))
        movement = StockMovement.objects.get(doc_ref=count.number)
        self.assertEqual(movement.movement_type, "shortage")
        self.assertEqual(movement.quantity_change, D("-2"))
        self.assertEqual(movement.balance_after, D("8"))

    def test_barcode_scan_increments_count_and_accepts_extra_barcode(self):
        ProductBarcode.objects.create(
            company=self.company, product=self.product, barcode="4820000001001",
        )
        count = self.create_count()
        response = self.client.post(
            f"/inventory/counts/{count.pk}/",
            {"barcode": "4820000001001", "action": "scan"},
        )
        self.assertEqual(response.status_code, 302)
        item = count.items.get(product=self.product)
        item.refresh_from_db()
        self.assertEqual(item.counted_quantity, D("1"))

    def test_approval_requires_at_least_one_counted_line(self):
        count = self.create_count()
        with self.assertRaises(StockError):
            services.approve_inventory_count(self.user, count, self.company)
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCount.DRAFT)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D("10"))

    def test_movements_after_snapshot_are_included_in_book_baseline(self):
        count = self.create_count()
        item = count.items.get(product=self.product)
        services.add_movement(self.product, D("2"), "purchase", "PUR-TEST", self.user, "Надходження під час підрахунку")
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D("12"))
        item.counted_quantity = D("12")
        item.save(update_fields=["counted_quantity"])
        services.approve_inventory_count(self.user, count, self.company)
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(item.book_quantity_at_approval, D("12"))
        self.assertEqual(item.difference, D("0"))
        self.assertEqual(self.product.current_stock, D("12"))
        self.assertEqual(StockMovement.objects.filter(doc_ref=count.number).count(), 0)

    def test_printable_inventory_sheet_is_scoped_to_company(self):
        count = self.create_count()
        response = self.client.get(f"/inventory/counts/{count.pk}/print/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, count.number)
        self.assertContains(response, self.product.name)
        self.assertContains(response, "Друкувати / зберегти PDF")

    def test_approved_document_cannot_be_changed_or_approved_twice(self):
        count = self.create_count()
        item = count.items.get(product=self.product)
        item.counted_quantity = D("10")
        item.save(update_fields=["counted_quantity"])
        services.approve_inventory_count(self.user, count, self.company)
        with self.assertRaises(StockError):
            services.approve_inventory_count(self.user, count, self.company)
        response = self.client.post(
            f"/inventory/counts/{count.pk}/",
            {f"counted_{item.pk}": "2", "action": "save"},
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.counted_quantity, D("10"))

    def test_detail_reports_surplus_shortage_and_filters_variances(self):
        second = Product.objects.create(
            company=self.company, name="Чай", sku="TEA-1", current_stock=D("5"),
        )
        count = self.create_count()
        first_item = count.items.get(product=self.product)
        second_item = count.items.get(product=second)
        first_item.counted_quantity = D("8")
        second_item.counted_quantity = D("7")
        InventoryCountItem.objects.bulk_update([first_item, second_item], ["counted_quantity"])

        response = self.client.get(f"/inventory/counts/{count.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["shortage"], D("2"))
        self.assertEqual(response.context["summary"]["surplus"], D("2"))
        self.assertEqual(response.context["summary"]["net_difference"], D("0"))

        response = self.client.get(f"/inventory/counts/{count.pk}/?status=shortage")
        self.assertEqual(list(response.context["items"].values_list("product_id", flat=True)), [self.product.pk])
        response = self.client.get(f"/inventory/counts/{count.pk}/?status=surplus")
        self.assertEqual(list(response.context["items"].values_list("product_id", flat=True)), [second.pk])
        response = self.client.get(f"/inventory/counts/{count.pk}/?status=difference")
        self.assertEqual(response.context["items"].count(), 2)

    def test_writeoff_requires_reason_and_explanation(self):
        self.client.force_login(self.user)
        base = {"product": self.product.pk, "kind": "writeoff", "quantity": "1.000", "reason": "", "note": ""}
        response = self.client.post("/inventory/adjust/", base)
        self.assertEqual(response.status_code, 200)
        self.assertIn("reason", response.context["form"].errors)
        self.assertIn("note", response.context["form"].errors)
        self.assertFalse(StockAdjustment.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D("10.000"))
        self.assertEqual(StockAdjustment.objects.count(), 0)

    def test_writeoff_records_reason_and_note_in_stock_movement_audit(self):
        self.client.force_login(self.user)
        response = self.client.post("/inventory/adjust/", {
            "product": self.product.pk, "kind": "writeoff", "quantity": "2.000",
            "reason": "damaged", "note": "Пошкоджена упаковка при прийманні",
        })
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D("8.000"))
        adjustment = StockAdjustment.objects.get()
        movement = StockMovement.objects.get(doc_ref=f"ADJ-{adjustment.pk:06d}")
        self.assertEqual(adjustment.reason, "damaged")
        self.assertEqual(movement.quantity_change, D("-2.000"))
        self.assertIn("Пошкодження", movement.note)
        self.assertIn("Пошкоджена упаковка при прийманні", movement.note)

    def test_movement_history_csv_export_respects_filters_and_has_utf8_bom(self):
        services.add_movement(self.product, D("3"), "purchase", "PUR-CSV", self.user, "CSV test")
        services.add_movement(self.product, D("-1"), "sale", "SALE-CSV", self.user, "CSV test")
        response = self.client.get("/inventory/movements/?type=purchase&export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"\\xef\\xbb\\xbf"))
        content = response.content.decode("utf-8-sig")
        self.assertIn("PUR-CSV", content)
        self.assertNotIn("SALE-CSV", content)
