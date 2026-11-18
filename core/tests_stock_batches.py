from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core import services
from core.models import Product, Supplier, StockBatch, BatchAllocation
from core.forms import ProductForm
from core.services import StockError
from core.tests_tenant_helpers import create_test_company


D = Decimal


class StockBatchFifoTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("batch-owner", "", "secret")
        self.company = create_test_company(self.user, "Batch store")
        self.supplier = Supplier.objects.create(company=self.company, name="Постачальник")
        self.product = Product.objects.create(
            company=self.company, name="Молоко", sku="MILK-1", price=D("45"),
            cost_price=D("25"), tracking_mode="expiry",
        )

    def purchase(self, qty, cost, batch, expiry):
        return services.create_purchase(
            self.user, self.supplier,
            [(self.product, D(qty), D(cost), batch, expiry)],
            company=self.company,
        )

    def checkout(self, qty):
        return services.finalize_sale(
            self.user, [(self.product, D(qty), self.product.price, None)],
            "cash", None, None, company=self.company,
        )

    def test_product_cannot_disable_batch_tracking_with_remaining_stock(self):
        expiry = timezone.localdate() + timedelta(days=10)
        self.purchase("2", "20", "ACTIVE", expiry)
        form = ProductForm({
            "name": self.product.name, "sku": self.product.sku, "barcode": "",
            "brand": "", "category": "", "unit": "шт", "product_kind": "goods",
            "tracking_mode": "none", "tax_category": "general", "condition": "new",
            "has_defect": "", "condition_description": "", "cost_price": "25", "price": "45",
            "min_stock": "0", "weight_kg": "", "length_cm": "", "width_cm": "", "height_cm": "",
            "additional_barcodes": "", "photo_url": "", "notes": "",
        }, instance=self.product, company=self.company)
        self.assertFalse(form.is_valid())
        self.assertIn("tracking_mode", form.errors)

    def test_serial_tracking_is_not_offered_before_serial_workflow_is_ready(self):
        form = ProductForm(company=self.company)
        values = [value for value, _label in form.fields["tracking_mode"].choices]
        self.assertNotIn("serial", values)

    def test_purchase_creates_expiry_batch_and_tracks_cost(self):
        expiry = timezone.localdate() + timedelta(days=30)
        purchase = self.purchase("10", "22.50", "LOT-001", expiry)
        batch = StockBatch.objects.get(purchase_item__purchase=purchase)
        self.product.refresh_from_db()
        self.assertEqual(batch.batch_number, "LOT-001")
        self.assertEqual(batch.expiry_date, expiry)
        self.assertEqual(batch.remaining_quantity, D("10"))
        self.assertEqual(batch.cost_price, D("22.50"))
        self.assertEqual(self.product.current_stock, D("10"))

    def test_expiry_mode_requires_expiry_date(self):
        with self.assertRaises(StockError):
            services.create_purchase(
                self.user, self.supplier,
                [(self.product, D("3"), D("20"), "NO-DATE", None)],
                company=self.company,
            )
        self.assertEqual(StockBatch.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D("0"))

    def test_checkout_consumes_fifo_and_return_restores_original_batch(self):
        today = timezone.localdate()
        self.purchase("3", "20", "EARLY", today + timedelta(days=5))
        self.purchase("5", "24", "LATE", today + timedelta(days=20))
        sale = self.checkout("4")
        item = sale.items.get()
        allocations = list(item.batch_allocations.select_related("batch").order_by("pk"))
        self.assertEqual([(a.batch.batch_number, a.quantity) for a in allocations],
                         [("EARLY", D("3")), ("LATE", D("1"))])
        self.assertEqual(item.cost_price, D("21.00"))
        early = StockBatch.objects.get(batch_number="EARLY")
        late = StockBatch.objects.get(batch_number="LATE")
        self.assertEqual((early.remaining_quantity, late.remaining_quantity), (D("0"), D("4")))

        services.create_return(self.user, sale, {item.pk: D("2")}, company=self.company)
        early.refresh_from_db()
        late.refresh_from_db()
        allocations[0].refresh_from_db()
        self.assertEqual((early.remaining_quantity, late.remaining_quantity), (D("2"), D("4")))
        self.assertEqual(allocations[0].returned_quantity, D("2"))

    def test_expired_batches_are_not_sold(self):
        today = timezone.localdate()
        self.purchase("2", "20", "EXPIRED", today - timedelta(days=1))
        with self.assertRaises(StockError):
            self.checkout("1")
        self.product.refresh_from_db()
        batch = StockBatch.objects.get(batch_number="EXPIRED")
        self.assertEqual(self.product.current_stock, D("2"))
        self.assertEqual(batch.remaining_quantity, D("2"))
        self.assertEqual(BatchAllocation.objects.count(), 0)

    def test_batch_monitor_page_filters_expired_and_soon_lots(self):
        today = timezone.localdate()
        self.purchase("2", "20", "OLD", today - timedelta(days=1))
        self.purchase("3", "21", "SOON", today + timedelta(days=7))
        self.client.force_login(self.user)
        expired = self.client.get("/inventory/batches/?status=expired")
        self.assertEqual(expired.status_code, 200)
        self.assertContains(expired, "OLD")
        self.assertNotContains(expired, "SOON")
        soon = self.client.get("/inventory/batches/?status=soon")
        self.assertContains(soon, "SOON")
        self.assertNotContains(soon, "OLD")

    def test_legacy_stock_is_migrated_to_unknown_date_batch_on_first_sale(self):
        self.product.current_stock = D("5")
        self.product.save(update_fields=["current_stock"])
        sale = self.checkout("1")
        self.assertEqual(sale.items.get().batch_allocations.count(), 1)
        legacy = StockBatch.objects.get(batch_number="LEGACY")
        self.assertIsNone(legacy.expiry_date)
        self.assertEqual(legacy.remaining_quantity, D("4"))
