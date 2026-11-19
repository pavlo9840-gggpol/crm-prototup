from decimal import Decimal
from datetime import timedelta

from django.utils import timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Product, Purchase, PurchaseItem, PurchaseOrder, PurchaseOrderItem, Supplier, SupplierClaim, SupplierItem, SupplierPriceChange, SupplierReturn, StockMovement, StockBatch
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

    def test_supplier_price_history_lists_only_current_company_changes(self):
        offer = SupplierItem.objects.create(
            company=self.company, supplier=self.supplier, product=self.product,
            brand="ACME", sku="OF-001", sku_norm=self.product.sku_norm,
            cost=Decimal("12.00"), available=True,
        )
        change = SupplierPriceChange.objects.create(
            company=self.company, item=offer,
            old_cost=Decimal("10.00"), new_cost=Decimal("12.00"),
        )
        other_owner = get_user_model().objects.create_superuser(
            username="supplier-history-other", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other history company", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Other history supplier")
        other_offer = SupplierItem.objects.create(
            company=other_company, supplier=other_supplier, brand="OTHER", sku="OTHER-1",
            sku_norm="OTHER1", cost=Decimal("3.00"),
        )
        SupplierPriceChange.objects.create(
            company=other_company, item=other_offer,
            old_cost=Decimal("2.00"), new_cost=Decimal("3.00"),
        )
        response = self.client.get(reverse("supplier_price_history"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_changes"], 1)
        self.assertEqual(response.context["changes"][0].pk, change.pk)
        self.assertEqual(response.context["changes"][0].delta, Decimal("2.00"))

    def test_supplier_price_history_filters_by_supplier_and_sku(self):
        offer = SupplierItem.objects.create(
            company=self.company, supplier=self.supplier, product=self.product,
            brand="ACME", sku="OF-001", sku_norm=self.product.sku_norm,
            cost=Decimal("12.00"), available=True,
        )
        SupplierPriceChange.objects.create(
            company=self.company, item=offer,
            old_cost=Decimal("10.00"), new_cost=Decimal("12.00"),
        )
        response = self.client.get(reverse("supplier_price_history"), {
            "supplier": str(self.supplier.pk), "q": "OF-001",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_changes"], 1)


    def test_supplier_analytics_compares_prices_and_delivery_performance(self):
        order, item = self.create_order(quantity="5", status=PurchaseOrder.CONFIRMED)
        order.expected_date = timezone.localdate() + timedelta(days=2)
        order.save(update_fields=["expected_date"])
        item.received_quantity = Decimal("5")
        item.save(update_fields=["received_quantity"])
        order.status = PurchaseOrder.RECEIVED
        order.save(update_fields=["status"])
        receipt = Purchase.objects.create(company=self.company, supplier=self.supplier, user=self.owner)
        PurchaseItem.objects.create(
            company=self.company, purchase=receipt, product=self.product,
            quantity=Decimal("5"), cost_price=Decimal("9.00"), order_item=item,
        )
        other_supplier = Supplier.objects.create(company=self.company, name="Supplier B")
        SupplierItem.objects.create(
            company=self.company, supplier=self.supplier, product=self.product,
            brand="ACME", sku="OF-001", sku_norm="OF001", cost=Decimal("8.50"), available=True,
        )
        SupplierItem.objects.create(
            company=self.company, supplier=other_supplier, product=self.product,
            brand="ACME", sku="OF-001", sku_norm="OF001", cost=Decimal("9.00"), available=True,
        )
        response = self.client.get(reverse("supplier_analytics"))
        self.assertEqual(response.status_code, 200)
        supplier_row = next(row for row in response.context["performance"] if row["supplier"].pk == self.supplier.pk)
        self.assertEqual(supplier_row["fill_rate"], Decimal("100.0"))
        self.assertEqual(supplier_row["avg_actual_cost"], Decimal("9.00"))
        self.assertEqual(supplier_row["price_delta"], Decimal("2.50"))
        self.assertEqual(supplier_row["on_time_rate"], Decimal("100.0"))
        self.assertIsNotNone(supplier_row["rating_score"])
        self.assertGreaterEqual(supplier_row["rating_score"], Decimal("0"))
        self.assertLessEqual(supplier_row["rating_score"], Decimal("100"))
        comparison = next(row for row in response.context["comparisons"] if row["product"].pk == self.product.pk)
        self.assertEqual([offer.cost for offer in comparison["offers"]], [Decimal("8.50"), Decimal("9.00")])

    def test_supplier_analytics_exports_filtered_csv(self):
        response = self.client.get(reverse("supplier_analytics"), {"export": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("supplier-analytics-", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(bytes((239, 187, 191))) )
        self.assertIn("Порівняння пропозицій".encode("utf-8"), response.content)

    def test_supplier_analytics_is_tenant_scoped(self):
        other_owner = get_user_model().objects.create_superuser(
            username="supplier-analytics-other", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other analytics company", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Secret supplier")
        other_product = Product.objects.create(company=other_company, name="Secret item", sku="SECRET-1")
        SupplierItem.objects.create(
            company=other_company, supplier=other_supplier, product=other_product,
            brand="SECRET", sku="SECRET-1", sku_norm="SECRET1", cost=Decimal("1.00"),
        )
        response = self.client.get(reverse("supplier_analytics"), {"q": "Secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["performance"], [])
        self.assertEqual(response.context["comparisons"], [])

    def test_supplier_claim_creation_requires_post(self):
        order, item = self.create_order(quantity="5", status=PurchaseOrder.CONFIRMED)
        response = self.client.get(reverse("supplier_claim_create", args=[order.pk, item.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertFalse(SupplierClaim.objects.filter(order=order).exists())

    def test_supplier_claim_lifecycle_is_audited(self):
        order, item = self.create_order(quantity="5", status=PurchaseOrder.CONFIRMED)
        response = self.client.post(
            reverse("supplier_claim_create", args=[order.pk, item.pk]),
            {"kind": "shortage", "quantity": "1", "requested_amount": "250.00",
             "description": "Пошкоджено корпус під час доставки"},
        )
        self.assertEqual(response.status_code, 302)
        claim = SupplierClaim.objects.get(order=order)
        self.assertEqual(claim.claim_number, f"CLM-{claim.pk:06d}")
        self.assertEqual(claim.status, SupplierClaim.OPEN)

        response = self.client.post(reverse("supplier_claim_update", args=[claim.pk]), {"action": "send"})
        claim.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(claim.status, SupplierClaim.SENT)
        self.assertIsNotNone(claim.sent_at)

        self.client.post(
            reverse("supplier_claim_update", args=[claim.pk]),
            {"action": "accept", "supplier_response": "Погоджено повернення коштів"},
        )
        claim.refresh_from_db()
        self.assertEqual(claim.status, SupplierClaim.ACCEPTED)
        self.assertEqual(claim.supplier_response, "Погоджено повернення коштів")

        self.client.post(
            reverse("supplier_claim_update", args=[claim.pk]),
            {"action": "resolve", "resolution_note": "Компенсацію погоджено та отримано"},
        )
        claim.refresh_from_db()
        self.assertEqual(claim.status, SupplierClaim.RESOLVED)
        self.assertEqual(claim.resolved_by, self.owner)
        self.assertIsNotNone(claim.closed_at)

    def test_supplier_claim_rejects_invalid_quantity_and_requires_reason(self):
        order, item = self.create_order(quantity="5", status=PurchaseOrder.CONFIRMED)
        self.client.post(
            reverse("supplier_claim_create", args=[order.pk, item.pk]),
            {"kind": "shortage", "quantity": "6", "description": "Не вистачає товару"},
        )
        self.assertFalse(SupplierClaim.objects.filter(order=order).exists())
        self.client.post(
            reverse("supplier_claim_create", args=[order.pk, item.pk]),
            {"kind": "shortage", "quantity": "1"},
        )
        self.assertFalse(SupplierClaim.objects.filter(order=order).exists())

    def test_supplier_claim_rejects_damaged_quantity_above_received(self):
        order, item = self.create_order(quantity="5", status=PurchaseOrder.CONFIRMED)
        self.client.post(
            reverse("supplier_claim_create", args=[order.pk, item.pk]),
            {"kind": "damaged", "quantity": "1", "description": "Пошкоджено"},
        )
        self.assertFalse(SupplierClaim.objects.filter(order=order).exists())
        item.received_quantity = Decimal("1")
        item.save(update_fields=["received_quantity"])
        self.client.post(
            reverse("supplier_claim_create", args=[order.pk, item.pk]),
            {"kind": "damaged", "quantity": "1", "description": "Пошкоджено"},
        )
        self.assertEqual(SupplierClaim.objects.filter(order=order).count(), 1)

    def test_supplier_claims_are_tenant_scoped(self):
        other_owner = get_user_model().objects.create_superuser(
            username="claim-other-owner", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other claims company", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Other supplier")
        other_product = Product.objects.create(company=other_company, name="Other product", sku="OTHER-CLAIM")
        other_order = PurchaseOrder.objects.create(
            company=other_company, supplier=other_supplier, user=other_owner, status=PurchaseOrder.SENT
        )
        other_item = PurchaseOrderItem.objects.create(
            company=other_company, order=other_order, product=other_product,
            quantity=Decimal("2"), cost_price=Decimal("5"),
        )
        foreign_claim = SupplierClaim.objects.create(
            company=other_company, order=other_order, order_item=other_item, supplier=other_supplier,
            kind="other", description="Private claim", created_by=other_owner,
        )
        response = self.client.get(reverse("supplier_claim_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, foreign_claim.claim_number)
        response = self.client.post(reverse("supplier_claim_update", args=[foreign_claim.pk]), {"action": "send"})
        self.assertEqual(response.status_code, 404)

    def _make_purchase_item_for_return(self, quantity="5", stock="5", tracking_mode="none"):
        self.product.tracking_mode = tracking_mode
        self.product.current_stock = Decimal(stock)
        self.product.save(update_fields=["tracking_mode", "current_stock"])
        purchase = Purchase.objects.create(
            company=self.company, supplier=self.supplier, user=self.owner,
            total=Decimal(quantity) * Decimal("10"),
        )
        item = PurchaseItem.objects.create(
            company=self.company, purchase=purchase, product=self.product,
            quantity=Decimal(quantity), cost_price=Decimal("10"),
        )
        return purchase, item

    def test_supplier_return_reduces_stock_and_writes_audited_movement(self):
        purchase, item = self._make_purchase_item_for_return()
        response = self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {"quantity": "2", "reason": "Пошкоджено під час доставки", "compensation_type": "credit"},
        )
        self.assertRedirects(response, reverse("purchase_detail", args=[purchase.pk]))
        returned = SupplierReturn.objects.get(purchase_item=item)
        self.assertEqual(returned.number, f"SR-{returned.pk:06d}")
        self.assertEqual(returned.total, Decimal("20"))
        self.assertEqual(returned.compensation_type, "credit")
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("3"))
        movement = StockMovement.objects.get(doc_ref=returned.number)
        self.assertEqual(movement.movement_type, "supplier_return")
        self.assertEqual(movement.quantity_change, Decimal("-2"))
        self.assertEqual(movement.balance_after, Decimal("3"))

    def test_supplier_return_cannot_exceed_received_quantity_or_repeat_return(self):
        purchase, item = self._make_purchase_item_for_return()
        self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {"quantity": "5", "reason": "Повернення всієї партії"},
        )
        self.assertEqual(SupplierReturn.objects.filter(purchase_item=item).count(), 1)
        self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {"quantity": "0.001", "reason": "Повторна спроба"},
        )
        self.assertEqual(SupplierReturn.objects.filter(purchase_item=item).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("0"))

    def test_supplier_return_requires_available_stock(self):
        purchase, item = self._make_purchase_item_for_return(stock="1")
        self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {"quantity": "2", "reason": "Немає достатнього залишку"},
        )
        self.assertFalse(SupplierReturn.objects.filter(purchase_item=item).exists())
        self.assertFalse(StockMovement.objects.filter(movement_type="supplier_return").exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("1"))

    def test_supplier_return_consumes_only_the_original_purchase_batch(self):
        purchase, item = self._make_purchase_item_for_return(tracking_mode="batch")
        batch = StockBatch.objects.create(
            company=self.company, product=self.product, purchase_item=item,
            received_quantity=Decimal("5"), remaining_quantity=Decimal("5"),
            cost_price=Decimal("10"), batch_number="LOT-1",
        )
        response = self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {"quantity": "2", "reason": "Повернення з партії"},
        )
        self.assertEqual(response.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(batch.remaining_quantity, Decimal("3"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("3"))

    def test_supplier_return_blocks_serial_tracked_goods_until_serial_selection_exists(self):
        purchase, item = self._make_purchase_item_for_return(tracking_mode="serial")
        self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {"quantity": "1", "reason": "Повернення серійного товару"},
        )
        self.assertFalse(SupplierReturn.objects.filter(purchase_item=item).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("5"))

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
