import json
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import CashOperation, Product, Sale, SaleItem, Shift, SaleReturn, Purchase, Supplier, StockMovement
from core.services import StockError, create_purchase, create_return, save_held


class FinancialIntegrityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="cashier", password="secret")
        self.admin = User.objects.create_superuser(username="admin", email="", password="secret")
        self.product = Product.objects.create(
            name="Oil filter",
            sku="OF-001",
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
            current_stock=Decimal("10.000"),
        )

    def test_second_return_cannot_exceed_remaining_quantity(self):
        sale = Sale.objects.create(
            number="CH-000001",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("60.00"),
        )
        item = SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity=Decimal("3.000"),
            original_price=Decimal("20.00"),
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
            returned_qty=Decimal("0.000"),
        )
        # The original sale has already reduced the stock by three.
        self.product.current_stock = Decimal("7.000")
        self.product.save(update_fields=["current_stock"])

        ret = create_return(self.admin, sale, {item.pk: Decimal("2.000")})
        self.assertEqual(ret.total, Decimal("40.00"))
        self.product.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("9.000"))
        self.assertEqual(item.returned_qty, Decimal("2.000"))

        with self.assertRaises(StockError):
            create_return(self.admin, sale, {item.pk: Decimal("2.000")})

        self.product.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("9.000"))
        self.assertEqual(item.returned_qty, Decimal("2.000"))
        self.assertEqual(SaleReturn.objects.filter(sale=sale).count(), 1)

    def test_return_rejects_unknown_mode_without_creating_records_or_changing_stock(self):
        sale = Sale.objects.create(
            number="CH-INVALID-MODE",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("20.00"),
        )
        item = SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity=Decimal("1.000"),
            original_price=Decimal("20.00"),
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
        )
        with self.assertRaises(StockError):
            create_return(self.admin, sale, {item.pk: Decimal("1.000")}, mode="invented")
        self.product.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("10.000"))
        self.assertEqual(item.returned_qty, Decimal("0.000"))
        self.assertEqual(SaleReturn.objects.filter(sale=sale).count(), 0)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 0)

    def test_reusing_a_completed_held_sale_does_not_create_second_sale(self):
        shift = Shift.objects.create(cashier=self.user, opening_cash=Decimal("0.00"), drawer={})
        held = save_held(
            self.user,
            [(self.product, Decimal("1.000"), Decimal("20.00"), None)],
            "cash",
            None,
            shift,
        )
        self.client.login(username="cashier", password="secret")
        payload = {
            "resume_id": held.pk,
            "items": [{"id": self.product.pk, "qty": "1"}],
            "payment": "cash",
            "received": "20.00",
        }

        first = self.client.post(
            "/pos/checkout/", json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.json().get("ok"), first.json())
        self.assertEqual(Sale.objects.filter(status=Sale.DONE).count(), 1)

        second = self.client.post(
            "/pos/checkout/", json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(second.status_code, 400, second.content)
        self.assertIn("чернетка", second.json()["error"].lower())
        self.assertEqual(Sale.objects.filter(status=Sale.DONE).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("9.000"))

    def test_cash_drawer_rejects_operation_with_wrong_direction(self):
        shift = Shift.objects.create(cashier=self.user, opening_cash=Decimal("0.00"), drawer={})
        self.client.login(username="cashier", password="secret")

        response = self.client.post("/drawer/", {"kind": "out", "c_1000": "1"})

        self.assertEqual(response.status_code, 302)
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {})
        self.assertEqual(CashOperation.objects.filter(shift=shift).count(), 0)

    def test_profit_report_excludes_unfulfilled_supplier_orders_and_nets_returns(self):
        self.client.login(username="admin", password="secret")
        pending_sale = Sale.objects.create(
            number="CH-000101",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("40.00"),
        )
        SaleItem.objects.create(
            sale=pending_sale,
            product=self.product,
            quantity=Decimal("2.000"),
            original_price=Decimal("20.00"),
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
            source=SaleItem.SUPPLIER,
            fulfil_status=SaleItem.ORDERED,
            earned_at=None,
        )

        issued_sale = Sale.objects.create(
            number="CH-000102",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("40.00"),
        )
        SaleItem.objects.create(
            sale=issued_sale,
            product=self.product,
            quantity=Decimal("2.000"),
            returned_qty=Decimal("1.000"),
            original_price=Decimal("20.00"),
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
            source=SaleItem.SUPPLIER,
            fulfil_status=SaleItem.ISSUED,
            earned_at=timezone.now(),
        )

        response = self.client.get("/reports/profit/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rev"], Decimal("20.00"))
        self.assertEqual(response.context["cost"], Decimal("12.00"))
        self.assertEqual(response.context["margin"], Decimal("8.00"))
        self.assertEqual(len(response.context["rows"]), 1)

    def test_profit_report_uses_handover_date_not_sale_creation_date(self):
        self.client.force_login(self.admin)
        sale = Sale.objects.create(
            number="CH-HANDOVER-DATE", status=Sale.DONE, cashier=self.user,
            total=Decimal("20.00"),
        )
        item = SaleItem.objects.create(
            sale=sale, product=self.product, quantity=Decimal("1.000"),
            original_price=Decimal("20.00"), price=Decimal("20.00"),
            cost_price=Decimal("12.00"), earned_at=timezone.now(),
        )
        # Simulate an order created in an earlier reporting period and handed
        # over today. Profit must appear in the handover period only.
        Sale.objects.filter(pk=sale.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        today = timezone.localdate()
        response = self.client.get("/reports/profit/", {
            "from": today.isoformat(), "to": today.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rev"], Decimal("20.00"))
        self.assertEqual(response.context["cost"], Decimal("12.00"))
        self.assertEqual(response.context["margin"], Decimal("8.00"))
        self.assertEqual(len(response.context["rows"]), 1)

        old_period = today - timedelta(days=40)
        response = self.client.get("/reports/profit/", {
            "from": old_period.isoformat(), "to": old_period.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rev"], Decimal("0"))
        self.assertEqual(response.context["cost"], Decimal("0"))
        self.assertEqual(response.context["margin"], Decimal("0"))
        self.assertEqual(len(response.context["rows"]), 0)

    def test_purchase_rejects_invalid_lines_without_creating_document_or_stock_movements(self):
        supplier = Supplier.objects.create(name="Test supplier")
        for quantity, cost in [
            (Decimal("0.000"), Decimal("12.00")),
            (Decimal("-1.000"), Decimal("12.00")),
            (Decimal("1.000"), Decimal("-0.01")),
        ]:
            with self.subTest(quantity=quantity, cost=cost):
                with self.assertRaises(StockError):
                    create_purchase(self.admin, supplier, [(self.product, quantity, cost)])
                self.product.refresh_from_db()
                self.assertEqual(self.product.current_stock, Decimal("10.000"))
                self.assertEqual(Purchase.objects.count(), 0)
                self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 0)

    def test_purchase_updates_stock_cost_history_and_movement_balances(self):
        supplier = Supplier.objects.create(name="Purchase lifecycle supplier")
        purchase = create_purchase(
            self.admin,
            supplier,
            [
                (self.product, Decimal("2.000"), Decimal("13.00")),
                # A duplicate line must use the newly persisted cost as its history baseline.
                (self.product, Decimal("3.000"), Decimal("15.00")),
            ],
        )
        self.assertEqual(purchase.total, Decimal("71.00"))
        self.assertEqual(purchase.items.count(), 2)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("15.000"))
        self.assertEqual(self.product.cost_price, Decimal("15.00"))

        moves = list(StockMovement.objects.filter(product=self.product, movement_type="purchase").order_by("pk"))
        self.assertEqual([m.quantity_change for m in moves], [Decimal("2.000"), Decimal("3.000")])
        self.assertEqual([m.balance_after for m in moves], [Decimal("12.000"), Decimal("15.000")])
        self.assertTrue(all(m.doc_ref == purchase.number for m in moves))

        history = list(self.product.price_history.filter(field="cost_price").order_by("pk"))
        self.assertEqual(
            [(h.old_value, h.new_value) for h in history],
            [(Decimal("12.00"), Decimal("13.00")), (Decimal("13.00"), Decimal("15.00"))],
        )

    def test_purchase_can_preserve_manual_cost_when_update_cost_is_disabled(self):
        supplier = Supplier.objects.create(name="Fixed cost supplier")
        purchase = create_purchase(
            self.admin, supplier, [(self.product, Decimal("1.000"), Decimal("14.00"))],
            update_cost=False,
        )
        self.product.refresh_from_db()
        self.assertEqual(purchase.total, Decimal("14.00"))
        self.assertEqual(self.product.current_stock, Decimal("11.000"))
        self.assertEqual(self.product.cost_price, Decimal("12.00"))
        self.assertFalse(self.product.price_history.filter(field="cost_price").exists())

    def test_supplier_return_for_unclaimed_order_does_not_change_stock(self):
        from core.models import SaleItem
        from core.services import return_to_supplier
        sale = Sale.objects.create(
            number="CH-UNCLAIMED-1", status=Sale.DONE, cashier=self.user,
            total=Decimal("20.00"),
        )
        item = SaleItem.objects.create(
            sale=sale, product=self.product, quantity=Decimal("1.000"),
            original_price=Decimal("20.00"), price=Decimal("20.00"),
            cost_price=Decimal("12.00"), source=SaleItem.SUPPLIER,
            fulfil_status=SaleItem.ARRIVED,
        )
        before_moves = StockMovement.objects.filter(product=self.product).count()
        return_to_supplier(self.admin, item)
        self.product.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("10.000"))
        self.assertEqual(item.returned_qty, Decimal("1.000"))
        self.assertEqual(item.fulfil_status, SaleItem.SUP_RETURN)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), before_moves)
        self.assertEqual(SaleReturn.objects.filter(sale=sale).count(), 1)

    def test_inventory_shortage_cannot_drive_stock_below_zero(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/inventory/adjust/",
            {
                "product": self.product.pk,
                "kind": "shortage",
                "quantity": "11.000",
                "reason": "other",
                "note": "Перевірка нестачі",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Недостатньо залишку")
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("10.000"))
        from core.models import StockAdjustment, StockMovement
        self.assertEqual(StockAdjustment.objects.count(), 0)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 0)

    def test_inventory_movement_history_searches_document_and_preserves_filters(self):
        StockMovement.objects.create(
            product=self.product, movement_type="purchase", quantity_change=Decimal("2.000"),
            balance_after=Decimal("12.000"), doc_ref="NK-TEST-42", user=self.admin,
        )
        StockMovement.objects.create(
            product=self.product, movement_type="sale", quantity_change=Decimal("-1.000"),
            balance_after=Decimal("11.000"), doc_ref="CH-TEST-42", user=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/inventory/movements/", {"q": "NK-TEST-42", "type": "purchase"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["movement_count"], 1)
        self.assertEqual(len(response.context["moves"]), 1)
        self.assertEqual(response.context["moves"][0].doc_ref, "NK-TEST-42")
        self.assertEqual(response.context["q"], "NK-TEST-42")
        self.assertEqual(response.context["selected_type"], "purchase")

    def test_inventory_adjustment_is_staff_only(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/inventory/adjust/",
            {
                "product": self.product.pk,
                "kind": "surplus",
                "quantity": "1.000",
                "reason": "other",
                "note": "Не має бути дозволено касиру",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("10.000"))
        from core.models import StockAdjustment, StockMovement
        self.assertEqual(StockAdjustment.objects.count(), 0)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 0)
