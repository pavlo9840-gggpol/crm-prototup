from decimal import Decimal
from datetime import timedelta

from django.utils import timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Product, Purchase, PurchaseItem, PurchaseOrder, PurchaseOrderItem, Supplier, SupplierItem
from core.tests_tenant_helpers import create_test_company


class SupplierPurchaseOrderTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="supplier-order-owner", email="", password="secret")
        self.company = create_test_company(self.owner, "Supplier order company", [])
        self.client.force_login(self.owner)
        self.supplier = Supplier.objects.create(company=self.company, name="Supplier A")
        self.product = Product.objects.create(
            company=self.company, name="Oil filter", sku="OF-001", brand="ACME",
            current_stock=Decimal("0"), min_stock=Decimal("2"), cost_price=Decimal("10"),
        )

    def create_order(self, quantity="5", status=PurchaseOrder.CONFIRMED):
        order = PurchaseOrder.objects.create(
            company=self.company, supplier=self.supplier, user=self.owner, status=status,
        )
        order.number = f"PO-{order.pk:06d}"
        order.save(update_fields=["number"])
        item = PurchaseOrderItem.objects.create(
            company=self.company, order=order, product=self.product,
            quantity=Decimal(quantity), cost_price=Decimal("8.50"),
        )
        return order, item

    def test_recommendations_subtract_open_supplier_orders(self):
        self.product.min_stock = Decimal("5")
        self.product.save()
        self.create_order(quantity="5", status=PurchaseOrder.SENT)
        response = self.client.get(reverse("purchase_recommendations"))
        self.assertEqual(response.status_code, 200)
        ids = {row["product"].pk for row in response.context["rows"]}
        self.assertNotIn(self.product.pk, ids)

    def test_recommendation_can_create_supplier_order(self):
        offer = SupplierItem.objects.create(
            company=self.company, supplier=self.supplier, product=self.product,
            brand=self.product.brand, sku=self.product.sku, sku_norm=self.product.sku_norm,
            cost=Decimal("8.50"), qty_avail=Decimal("10"), available=True,
        )
        response = self.client.post(reverse("purchase_order_create"), {
            "product": [str(self.product.pk)], "qty": ["4"], "offer": [str(offer.pk)],
        })
        self.assertRedirects(response, reverse("purchase_order_list"))
        order = PurchaseOrder.objects.get(company=self.company, supplier=self.supplier)
        self.assertEqual(order.status, PurchaseOrder.DRAFT)
        item = order.items.get()
        self.assertEqual(item.quantity, Decimal("4"))
        self.assertEqual(item.cost_price, Decimal("8.50"))
        self.assertEqual(item.supplier_item, offer)

    def test_supplier_order_partial_receipt_creates_real_purchase_and_stock(self):
        order, item = self.create_order(quantity="5")
        response = self.client.post(
            reverse("purchase_order_receive", args=[order.pk, item.pk]),
            {"quantity": "2", "actual_cost": "9.25"},
        )
        self.assertRedirects(response, reverse("purchase_order_detail", args=[order.pk]))
        item.refresh_from_db()
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(item.received_quantity, Decimal("2"))
        self.assertEqual(item.outstanding_quantity, Decimal("3"))
        self.assertEqual(order.status, PurchaseOrder.PARTIAL)
        self.assertEqual(self.product.current_stock, Decimal("2"))
        receipt_item = PurchaseItem.objects.get(order_item=item)
        self.assertEqual(receipt_item.quantity, Decimal("2"))
        self.assertEqual(receipt_item.cost_price, Decimal("9.25"))
        self.assertTrue(Purchase.objects.filter(pk=receipt_item.purchase_id, company=self.company).exists())

    def test_receipt_rejects_negative_actual_supplier_price(self):
        order, item = self.create_order(quantity="2")
        self.client.post(
            reverse("purchase_order_receive", args=[order.pk, item.pk]),
            {"quantity": "1", "actual_cost": "-1"},
        )
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(item.received_quantity, Decimal("0"))
        self.assertEqual(self.product.current_stock, Decimal("0"))
        self.assertFalse(PurchaseItem.objects.filter(order_item=item).exists())

    def test_final_receipt_closes_order(self):
        order, item = self.create_order(quantity="3")
        self.client.post(
            reverse("purchase_order_receive", args=[order.pk, item.pk]),
            {"quantity": "3"},
        )
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.RECEIVED)
        self.assertIsNotNone(order.closed_at)

    def test_receipt_cannot_exceed_ordered_quantity(self):
        order, item = self.create_order(quantity="2")
        self.client.post(
            reverse("purchase_order_receive", args=[order.pk, item.pk]),
            {"quantity": "3"},
        )
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(item.received_quantity, Decimal("0"))
        self.assertEqual(self.product.current_stock, Decimal("0"))

    def test_draft_order_can_save_expected_date_and_supplier_note(self):
        order, _item = self.create_order(status=PurchaseOrder.DRAFT)
        response = self.client.post(
            reverse("purchase_order_update", args=[order.pk]),
            {"expected_date": "2026-12-05", "note": "Доставити до обіду"},
        )
        self.assertRedirects(response, reverse("purchase_order_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(str(order.expected_date), "2026-12-05")
        self.assertEqual(order.note, "Доставити до обіду")

    def test_sent_order_cannot_be_edited(self):
        order, _item = self.create_order(status=PurchaseOrder.SENT)
        response = self.client.post(
            reverse("purchase_order_update", args=[order.pk]),
            {"expected_date": "2026-12-05", "note": "Змінена примітка"},
        )
        self.assertRedirects(response, reverse("purchase_order_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertIsNone(order.expected_date)
        self.assertEqual(order.note, "")

    def test_order_print_is_available_and_contains_order_lines(self):
        order, item = self.create_order(quantity="3")
        response = self.client.get(reverse("purchase_order_print", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.number)
        self.assertContains(response, self.product.sku)
        self.assertContains(response, "Друк")

    def test_order_csv_export_contains_all_order_lines_and_totals(self):
        order, item = self.create_order(quantity="3")
        response = self.client.get(reverse("purchase_order_export_csv", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("utf-8-sig", response["Content-Type"])
        content = response.content.decode("utf-8-sig")
        self.assertIn(self.product.sku, content)
        self.assertIn("25.50", content)

    def test_delivery_control_shows_overdue_and_remaining_quantities(self):
        overdue, overdue_item = self.create_order(quantity="5", status=PurchaseOrder.PARTIAL)
        overdue_item.received_quantity = Decimal("2")
        overdue_item.save(update_fields=["received_quantity"])
        overdue.expected_date = timezone.localdate() - timedelta(days=3)
        overdue.save(update_fields=["expected_date"])
        upcoming, _upcoming_item = self.create_order(quantity="4", status=PurchaseOrder.CONFIRMED)
        upcoming.expected_date = timezone.localdate() + timedelta(days=2)
        upcoming.save(update_fields=["expected_date"])

        response = self.client.get(reverse("purchase_order_control"))
        self.assertEqual(response.status_code, 200)
        rows = {row.pk: row for row in response.context["orders"]}
        self.assertIn(overdue.pk, rows)
        self.assertIn(upcoming.pk, rows)
        self.assertTrue(rows[overdue.pk].is_overdue)
        self.assertEqual(rows[overdue.pk].outstanding_quantity, Decimal("3"))
        self.assertFalse(rows[upcoming.pk].is_overdue)
        self.assertEqual(response.context["overdue_count"], 1)
        self.assertEqual(response.context["incomplete_count"], 2)

    def test_delivery_control_overdue_filter_and_tenant_isolation(self):
        overdue, _item = self.create_order(quantity="2", status=PurchaseOrder.SENT)
        overdue.expected_date = timezone.localdate() - timedelta(days=1)
        overdue.save(update_fields=["expected_date"])
        other_owner = get_user_model().objects.create_superuser(
            username="supplier-control-other", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other control company", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Other supplier")
        other_order = PurchaseOrder.objects.create(
            company=other_company, supplier=other_supplier, user=other_owner,
            status=PurchaseOrder.SENT, expected_date=timezone.localdate() - timedelta(days=2),
        )
        other_product = Product.objects.create(
            company=other_company, name="Other product", sku="OTHER-1",
            current_stock=Decimal("0"), cost_price=Decimal("1"),
        )
        PurchaseOrderItem.objects.create(
            company=other_company, order=other_order, product=other_product,
            quantity=Decimal("1"), cost_price=Decimal("1"),
        )
        response = self.client.get(reverse("purchase_order_control"), {"delivery": "overdue"})
        self.assertEqual(response.status_code, 200)
        ids = {row.pk for row in response.context["orders"]}
        self.assertEqual(ids, {overdue.pk})
        self.assertNotIn(other_order.pk, ids)

    def test_foreign_company_order_is_not_visible(self):
        other_owner = get_user_model().objects.create_superuser(
            username="supplier-order-other", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other supplier order company", [])
        other_order = PurchaseOrder.objects.create(
            company=other_company,
            supplier=Supplier.objects.create(company=other_company, name="Other supplier"),
            user=other_owner,
        )
        response = self.client.get(reverse("purchase_order_detail", args=[other_order.pk]))
        self.assertEqual(response.status_code, 404)
