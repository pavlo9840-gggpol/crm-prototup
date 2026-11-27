import json
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.db.models import Sum
from django.urls import reverse

from core.models import BankAccount, BankTransaction, CashOperation, Product, Sale, SaleItem, Shift, SaleReturn, Purchase, Supplier, StockMovement, SupplierReturn, StockSerialUnit, SupplierReplacementReceipt, SupplierCompensation
from core.services import StockError, _lock_products, create_purchase, create_return, save_held
from core.tests_tenant_helpers import create_test_company


class FinancialIntegrityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="cashier", password="secret")
        self.admin = User.objects.create_superuser(username="admin", email="", password="secret")
        self.company = create_test_company(self.admin, "Financial integrity shop", [(self.user, "cashier")])
        self.product = Product.objects.create(company=self.company, 
            name="Oil filter",
            sku="OF-001",
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
            current_stock=Decimal("10.000"),
        )

    def test_multi_product_stock_locks_are_acquired_in_primary_key_order(self):
        other = Product.objects.create(company=self.company, 
            name="Air filter", sku="AF-002", price=Decimal("30.00"),
            cost_price=Decimal("18.00"), current_stock=Decimal("5.000"),
        )
        requested = [other.pk, self.product.pk, other.pk]
        with CaptureQueriesContext(connection) as queries:
            locked = _lock_products(requested)

        self.assertEqual(list(locked), sorted({other.pk, self.product.pk}))
        lock_query = next(
            query["sql"] for query in queries
            if "FROM \"core_product\"" in query["sql"] and " IN " in query["sql"]
        )
        self.assertIn("ORDER BY", lock_query)
        self.assertLess(lock_query.index("ORDER BY"), len(lock_query))

    def test_second_return_cannot_exceed_remaining_quantity(self):
        sale = Sale.objects.create(company=self.company, 
            number="CH-000001",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("60.00"),
        )
        item = SaleItem.objects.create(company=self.company, 
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

    def test_cash_return_updates_drawer_and_records_cash_operation(self):
        sale = Sale.objects.create(company=self.company, number="CH-CASH-RETURN", status=Sale.DONE, cashier=self.user,
                                   total=Decimal("20.00"))
        item = SaleItem.objects.create(company=self.company, sale=sale, product=self.product, quantity=Decimal("1.000"),
                                       original_price=Decimal("20.00"), price=Decimal("20.00"),
                                       cost_price=Decimal("12.00"))
        shift = Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("40.00"), drawer={"2000": 2})

        ret = create_return(self.admin, sale, {item.pk: Decimal("1.000")},
                            refund_method="cash", shift=shift)

        shift.refresh_from_db()
        self.assertEqual(ret.refund_method, "cash")
        self.assertEqual(ret.cash_amount, Decimal("20.00"))
        self.assertEqual(ret.card_amount, Decimal("0.00"))
        self.assertEqual(ret.shift_id, shift.pk)
        self.assertEqual(shift.drawer, {"2000": 1})
        op = CashOperation.objects.get(shift=shift)
        self.assertEqual(op.kind, "out")
        self.assertEqual(op.amount, Decimal("-20.00"))
        self.assertEqual(op.detail, {"2000": -1})

    def test_card_return_does_not_require_or_change_cash_shift(self):
        sale = Sale.objects.create(company=self.company, number="CH-CARD-RETURN", status=Sale.DONE, cashier=self.user,
                                   total=Decimal("20.00"))
        item = SaleItem.objects.create(company=self.company, sale=sale, product=self.product, quantity=Decimal("1.000"),
                                       original_price=Decimal("20.00"), price=Decimal("20.00"),
                                       cost_price=Decimal("12.00"))

        ret = create_return(self.admin, sale, {item.pk: Decimal("1.000")}, refund_method="card")

        self.assertEqual(ret.card_amount, Decimal("20.00"))
        self.assertEqual(ret.cash_amount, Decimal("0.00"))
        self.assertIsNone(ret.shift_id)
        self.assertEqual(CashOperation.objects.count(), 0)

    def test_cash_return_without_exact_drawer_funds_rolls_back_stock_and_return(self):
        sale = Sale.objects.create(company=self.company, number="CH-NO-CASH", status=Sale.DONE, cashier=self.user,
                                   total=Decimal("20.00"))
        item = SaleItem.objects.create(company=self.company, sale=sale, product=self.product, quantity=Decimal("1.000"),
                                       original_price=Decimal("20.00"), price=Decimal("20.00"),
                                       cost_price=Decimal("12.00"))
        shift = Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("100.00"), drawer={"10000": 1})

        with self.assertRaises(StockError):
            create_return(self.admin, sale, {item.pk: Decimal("1.000")},
                          refund_method="cash", shift=shift)

        self.product.refresh_from_db()
        item.refresh_from_db()
        shift.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("10.000"))
        self.assertEqual(item.returned_qty, Decimal("0.000"))
        self.assertEqual(shift.drawer, {"10000": 1})
        self.assertEqual(SaleReturn.objects.filter(sale=sale).count(), 0)
        self.assertEqual(CashOperation.objects.count(), 0)

    def test_mixed_return_splits_cash_and_card_exactly(self):
        sale = Sale.objects.create(company=self.company, number="CH-MIX-RETURN", status=Sale.DONE, cashier=self.user,
                                   total=Decimal("20.00"))
        item = SaleItem.objects.create(company=self.company, sale=sale, product=self.product, quantity=Decimal("1.000"),
                                       original_price=Decimal("20.00"), price=Decimal("20.00"),
                                       cost_price=Decimal("12.00"))
        shift = Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("10.00"), drawer={"1000": 1})

        ret = create_return(self.admin, sale, {item.pk: Decimal("1.000")},
                            refund_method="mixed", refund_cash_amount=Decimal("10.00"), shift=shift)

        self.assertEqual(ret.cash_amount, Decimal("10.00"))
        self.assertEqual(ret.card_amount, Decimal("10.00"))
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {})
        self.assertEqual(CashOperation.objects.get(shift=shift).amount, Decimal("-10.00"))

    def test_pending_return_can_be_settled_to_card_once(self):
        from core.services import settle_return
        sale = Sale.objects.create(company=self.company, number="CH-PENDING-RETURN", status=Sale.DONE, cashier=self.user,
                                   total=Decimal("20.00"))
        item = SaleItem.objects.create(company=self.company, sale=sale, product=self.product, quantity=Decimal("1.000"),
                                       original_price=Decimal("20.00"), price=Decimal("20.00"),
                                       cost_price=Decimal("12.00"))
        ret = create_return(self.admin, sale, {item.pk: Decimal("1.000")})
        self.assertEqual(ret.refund_method, "pending")

        settled = settle_return(self.admin, ret, "card")

        self.assertEqual(settled.refund_method, "card")
        self.assertEqual(settled.card_amount, Decimal("20.00"))
        with self.assertRaises(StockError):
            settle_return(self.admin, ret, "card")
        self.assertEqual(CashOperation.objects.count(), 0)

    def test_pending_return_cash_settlement_decrements_active_drawer(self):
        from core.services import settle_return
        sale = Sale.objects.create(company=self.company, number="CH-PENDING-CASH", status=Sale.DONE, cashier=self.user,
                                   total=Decimal("10.00"))
        item = SaleItem.objects.create(company=self.company, sale=sale, product=self.product, quantity=Decimal("0.500"),
                                       original_price=Decimal("20.00"), price=Decimal("20.00"),
                                       cost_price=Decimal("12.00"))
        ret = create_return(self.admin, sale, {item.pk: Decimal("0.500")})
        shift = Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("10.00"), drawer={"1000": 1})

        settled = settle_return(self.admin, ret, "cash", shift=shift)

        shift.refresh_from_db()
        self.assertEqual(settled.cash_amount, Decimal("10.00"))
        self.assertEqual(shift.drawer, {})
        self.assertEqual(CashOperation.objects.get(shift=shift).amount, Decimal("-10.00"))

    def test_return_rejects_unknown_mode_without_creating_records_or_changing_stock(self):
        sale = Sale.objects.create(company=self.company, 
            number="CH-INVALID-MODE",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("20.00"),
        )
        item = SaleItem.objects.create(company=self.company, 
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
        shift = Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("0.00"), drawer={})
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

    def test_pos_checkout_rounds_the_combined_total_once_for_card_payments(self):
        second = Product.objects.create(company=self.company, 
            name="Air filter", sku="AF-ROUND-01", price=Decimal("10.01"),
            cost_price=Decimal("5.00"), current_stock=Decimal("1.000"),
        )
        self.product.price = Decimal("10.01")
        self.product.current_stock = Decimal("1.000")
        self.product.save(update_fields=["price", "current_stock"])
        Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("0.00"), drawer={})
        self.client.login(username="cashier", password="secret")

        response = self.client.post(
            "/pos/checkout/",
            json.dumps({
                "items": [
                    {"id": self.product.pk, "qty": "0.333"},
                    {"id": second.pk, "qty": "0.333"},
                ],
                "payment": "card",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json().get("ok"), response.json())
        sale = Sale.objects.get(pk=response.json()["id"])
        self.assertEqual(sale.total, Decimal("6.67"))
        self.assertEqual(sale.card_amount, sale.total)
        self.assertEqual(sale.cash_amount, Decimal("0.00"))

    def test_cash_drawer_rejects_operation_with_wrong_direction(self):
        shift = Shift.objects.create(company=self.company, cashier=self.user, opening_cash=Decimal("0.00"), drawer={})
        self.client.login(username="cashier", password="secret")

        response = self.client.post("/drawer/", {"kind": "out", "c_1000": "1"})

        self.assertEqual(response.status_code, 302)
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {})
        self.assertEqual(CashOperation.objects.filter(shift=shift).count(), 0)

    def test_profit_report_excludes_unfulfilled_supplier_orders_and_nets_returns(self):
        self.client.login(username="admin", password="secret")
        pending_sale = Sale.objects.create(company=self.company, 
            number="CH-000101",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("40.00"),
        )
        SaleItem.objects.create(company=self.company, 
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

        issued_sale = Sale.objects.create(company=self.company, 
            number="CH-000102",
            status=Sale.DONE,
            cashier=self.user,
            total=Decimal("40.00"),
        )
        SaleItem.objects.create(company=self.company, 
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
        sale = Sale.objects.create(company=self.company, 
            number="CH-HANDOVER-DATE", status=Sale.DONE, cashier=self.user,
            total=Decimal("20.00"),
        )
        item = SaleItem.objects.create(company=self.company, 
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

    def test_daily_and_monthly_net_revenue_subtracts_returns_in_return_period(self):
        self.client.force_login(self.admin)
        sale = Sale.objects.create(company=self.company, 
            number="CH-REPORT-RETURN", status=Sale.DONE, cashier=self.user,
            total=Decimal("40.00"),
        )
        item = SaleItem.objects.create(company=self.company, 
            sale=sale, product=self.product, quantity=Decimal("2.000"),
            original_price=Decimal("20.00"), price=Decimal("20.00"),
            cost_price=Decimal("12.00"), earned_at=timezone.now(),
        )
        ret = create_return(self.admin, sale, {item.pk: Decimal("1.000")})
        today = timezone.localdate()
        daily = self.client.get("/reports/daily/", {"date": today.isoformat()})
        self.assertEqual(daily.status_code, 200)
        self.assertEqual(daily.context["total"], Decimal("40.00"))
        self.assertEqual(daily.context["returns_total"], ret.total)
        self.assertEqual(daily.context["net_total"], Decimal("20.00"))

        monthly = self.client.get("/reports/monthly/", {"month": today.strftime("%Y-%m")})
        self.assertEqual(monthly.status_code, 200)
        self.assertEqual(monthly.context["gross_total"], Decimal("40.00"))
        self.assertEqual(monthly.context["returns_total"], ret.total)
        self.assertEqual(monthly.context["total"], Decimal("20.00"))

    def test_purchase_rejects_invalid_lines_without_creating_document_or_stock_movements(self):
        supplier = Supplier.objects.create(company=self.company, name="Test supplier")
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
        supplier = Supplier.objects.create(company=self.company, name="Purchase lifecycle supplier")
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
        supplier = Supplier.objects.create(company=self.company, name="Fixed cost supplier")
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
        sale = Sale.objects.create(company=self.company, 
            number="CH-UNCLAIMED-1", status=Sale.DONE, cashier=self.user,
            total=Decimal("20.00"),
        )
        item = SaleItem.objects.create(company=self.company, 
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
            company=self.company, product=self.product, movement_type="purchase", quantity_change=Decimal("2.000"),
            balance_after=Decimal("12.000"), doc_ref="NK-TEST-42", user=self.admin,
        )
        StockMovement.objects.create(
            company=self.company, product=self.product, movement_type="sale", quantity_change=Decimal("-1.000"),
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


    def test_supplier_replacement_receipt_links_serial_units_without_new_payable(self):
        self.client.force_login(self.admin)
        supplier = Supplier.objects.create(company=self.company, name="Serial replacement supplier")
        original_purchase = create_purchase(
            self.admin, supplier,
            [(self.product, Decimal("1.000"), Decimal("12.00"))],
        )
        self.product.tracking_mode = "serial"
        self.product.save(update_fields=["tracking_mode"])
        original_item = original_purchase.items.get()
        returned = SupplierReturn.objects.create(
            company=self.company, purchase_item=original_item, supplier=supplier,
            quantity=Decimal("1.000"), unit_cost=Decimal("12.00"),
            compensation_type="replacement", reason="Гарантійна заміна",
            completed_at=timezone.now(), created_by=self.admin,
        )

        response = self.client.post(
            reverse("supplier_replacement_receive", args=[returned.pk]),
            {"quantity": "1.000", "serial_numbers": "REPL-SN-001", "note": "Тестова заміна"},
        )
        self.assertEqual(response.status_code, 302)
        receipt = SupplierReplacementReceipt.objects.get(supplier_return=returned)
        replacement_item = receipt.purchase_item
        self.assertEqual(receipt.quantity, Decimal("1.000"))
        self.assertEqual(receipt.purchase.total, Decimal("0.00"))
        self.assertEqual(replacement_item.cost_price, Decimal("12.00"))
        serial = StockSerialUnit.objects.get(normalized_serial="REPLSN001")
        self.assertEqual(serial.purchase_item_id, replacement_item.pk)
        self.assertEqual(serial.product_id, self.product.pk)


    def test_supplier_return_stock_and_credit_compensation_complete_end_to_end(self):
        self.client.force_login(self.admin)
        supplier = Supplier.objects.create(company=self.company, name="End-to-end supplier return")
        purchase = create_purchase(
            self.admin, supplier,
            [(self.product, Decimal("2.000"), Decimal("12.00"))],
        )
        item = purchase.items.get()
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("12.000"))
        self.assertEqual(purchase.total, Decimal("24.00"))

        response = self.client.post(
            reverse("supplier_return_create", args=[purchase.pk, item.pk]),
            {
                "quantity": "1.000",
                "reason": "Пошкоджена одиниця",
                "compensation_type": "credit",
                "note": "Наскрізна перевірка повернення",
            },
        )
        self.assertEqual(response.status_code, 302)
        returned = SupplierReturn.objects.get(purchase_item=item)
        self.assertEqual(returned.quantity, Decimal("1.000"))
        self.assertEqual(returned.total, Decimal("12.000"))

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("11.000"))
        movements = list(
            StockMovement.objects.filter(
                product=self.product,
                doc_ref__in=[purchase.number, returned.number],
            ).order_by("pk")
        )
        self.assertEqual(
            [(row.movement_type, row.quantity_change, row.balance_after) for row in movements],
            [
                ("purchase", Decimal("2.000"), Decimal("12.000")),
                ("supplier_return", Decimal("-1.000"), Decimal("11.000")),
            ],
        )

        response = self.client.post(
            reverse("supplier_return_compensate", args=[returned.pk]),
            {"kind": "credit", "amount": "12.00", "note": "Зарахування повернення"},
        )
        self.assertEqual(response.status_code, 302)
        compensation = SupplierCompensation.objects.get(supplier_return=returned, is_void=False)
        self.assertEqual(compensation.amount, Decimal("12.00"))
        self.assertEqual(compensation.kind, SupplierCompensation.CREDIT)

        # The credit note reduces the payable, but it must not affect stock a
        # second time: stock was already reduced when the return was posted.
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("11.000"))
        self.assertEqual(
            StockMovement.objects.filter(product=self.product, movement_type="supplier_return").count(),
            1,
        )
        balances = self.client.get(reverse("supplier_balances_report"))
        self.assertEqual(balances.status_code, 200)
        self.assertEqual(balances.context["totals"]["debt"], Decimal("12.00"))
        controls = self.client.get(reverse("finance_controls"))
        self.assertEqual(controls.status_code, 200)
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("12.00"))
        self.assertEqual(
            controls.context["supplier_debt_total"],
            "12.00",
        )

    def test_supplier_cash_compensation_void_posts_equal_outflow_and_preserves_audit(self):
        self.client.force_login(self.admin)
        supplier = Supplier.objects.create(company=self.company, name="Cash compensation supplier")
        purchase = create_purchase(
            self.admin, supplier,
            [(self.product, Decimal("1.000"), Decimal("200.00"))],
        )
        returned = SupplierReturn.objects.create(
            company=self.company, purchase_item=purchase.items.get(), supplier=supplier,
            quantity=Decimal("1.000"), unit_cost=Decimal("200.00"),
            compensation_type="cash", reason="Повернення готівкою",
            completed_at=timezone.now(), created_by=self.admin,
        )
        shift = Shift.objects.create(company=self.company, cashier=self.admin, drawer={"20000": 1})
        response = self.client.post(
            reverse("supplier_return_compensate", args=[returned.pk]),
            {"kind": "cash", "amount": "200.00", "note": "Готівкове повернення"},
        )
        self.assertEqual(response.status_code, 302)
        compensation = SupplierCompensation.objects.get(supplier_return=returned, is_void=False)
        original = compensation.cash_operation
        self.assertEqual(original.kind, "in")
        self.assertEqual(original.amount, Decimal("200.00"))
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {"20000": 2})
        from core.views import shift_summary
        self.assertEqual(shift_summary(shift)["supplier_compensation_net"], Decimal("200.00"))

        response = self.client.post(
            reverse("supplier_compensation_void", args=[compensation.pk]),
            {"reason": "Помилкова виплата"},
        )
        self.assertEqual(response.status_code, 302)
        compensation.refresh_from_db()
        shift.refresh_from_db()
        self.assertTrue(compensation.is_void)
        self.assertEqual(compensation.void_reason, "Помилкова виплата")
        self.assertIsNotNone(compensation.voided_at)
        self.assertEqual(compensation.voided_by, self.admin)
        self.assertEqual(compensation.reversal_cash_operation.kind, "out")
        self.assertEqual(compensation.reversal_cash_operation.amount, Decimal("-200.00"))
        self.assertEqual(shift.drawer, {"20000": 1})
        self.assertEqual(shift_summary(shift)["supplier_compensation_net"], Decimal("0.00"))
        cashflow = self.client.get(reverse("cashflow_report"))
        self.assertEqual(cashflow.status_code, 200)
        self.assertEqual(cashflow.context["cash_in"], Decimal("200.00"))
        self.assertEqual(cashflow.context["cash_out"], Decimal("200.00"))
        self.assertEqual(cashflow.context["cash_net"], Decimal("0.00"))
        pnl = self.client.get(reverse("finance_overview"))
        self.assertEqual(pnl.status_code, 200)
        self.assertEqual(pnl.context["cash_in"], Decimal("200.00"))
        self.assertEqual(pnl.context["cash_out"], Decimal("200.00"))
        self.assertEqual(pnl.context["operating_result"], Decimal("0.00"))
        self.assertEqual(pnl.context["revenue"], Decimal("0.00"))
        self.assertEqual(pnl.context["expenses"], Decimal("0.00"))
        controls = self.client.get(reverse("finance_controls"))
        self.assertEqual(controls.context["cash_period_in"], Decimal("200.00"))
        self.assertEqual(controls.context["cash_period_out"], Decimal("200.00"))
        self.assertEqual(controls.context["period_cash_net"], Decimal("0.00"))
        self.assertEqual(controls.context["period_cash_net"], Decimal("0.00"))
        self.assertEqual(CashOperation.objects.filter(
            shift=shift, detail__source="supplier_return_compensation"
        ).count(), 1)
        self.assertEqual(CashOperation.objects.filter(
            shift=shift, detail__source="supplier_compensation_reversal"
        ).count(), 1)

    def test_supplier_cash_compensation_cannot_be_voided_after_shift_close(self):
        self.client.force_login(self.admin)
        supplier = Supplier.objects.create(company=self.company, name="Closed shift supplier")
        purchase = create_purchase(
            self.admin, supplier,
            [(self.product, Decimal("1.000"), Decimal("200.00"))],
        )
        returned = SupplierReturn.objects.create(
            company=self.company, purchase_item=purchase.items.get(), supplier=supplier,
            quantity=Decimal("1.000"), unit_cost=Decimal("200.00"),
            compensation_type="cash", reason="Повернення",
            completed_at=timezone.now(), created_by=self.admin,
        )
        shift = Shift.objects.create(company=self.company, cashier=self.admin, drawer={"20000": 1})
        self.client.post(reverse("supplier_return_compensate", args=[returned.pk]), {
            "kind": "cash", "amount": "200.00", "note": "Готівка",
        })
        compensation = SupplierCompensation.objects.get(supplier_return=returned, is_void=False)
        shift.closed_at = timezone.now()
        shift.save(update_fields=["closed_at"])
        response = self.client.post(
            reverse("supplier_compensation_void", args=[compensation.pk]),
            {"reason": "Після закриття"},
        )
        self.assertEqual(response.status_code, 302)
        compensation.refresh_from_db()
        self.assertFalse(compensation.is_void)
        self.assertIsNone(compensation.reversal_cash_operation_id)

    def test_supplier_bank_compensation_void_creates_balancing_bank_entry(self):
        self.client.force_login(self.admin)
        supplier = Supplier.objects.create(company=self.company, name="Bank compensation supplier")
        purchase = create_purchase(
            self.admin, supplier,
            [(self.product, Decimal("1.000"), Decimal("200.00"))],
        )
        returned = SupplierReturn.objects.create(
            company=self.company, purchase_item=purchase.items.get(), supplier=supplier,
            quantity=Decimal("1.000"), unit_cost=Decimal("200.00"),
            compensation_type="bank", reason="Повернення на рахунок",
            completed_at=timezone.now(), created_by=self.admin,
        )
        account = BankAccount.objects.create(
            company=self.company, bank_name="Тестовий банк",
            iban="UA123456789012345678901234567", currency="UAH",
        )
        response = self.client.post(
            reverse("supplier_return_compensate", args=[returned.pk]),
            {"kind": "bank", "amount": "200.00", "bank_account": account.pk, "note": "Повернення"},
        )
        self.assertEqual(response.status_code, 302)
        compensation = SupplierCompensation.objects.get(supplier_return=returned, is_void=False)
        original = compensation.bank_transaction
        self.assertEqual(original.direction, BankTransaction.INCOME)
        self.assertEqual(original.amount, Decimal("200.00"))

        response = self.client.post(
            reverse("supplier_compensation_void", args=[compensation.pk]),
            {"reason": "Помилкове зарахування"},
        )
        self.assertEqual(response.status_code, 302)
        compensation.refresh_from_db()
        self.assertTrue(compensation.is_void)
        reversal = compensation.reversal_bank_transaction
        self.assertEqual(reversal.direction, BankTransaction.EXPENSE)
        self.assertEqual(reversal.amount, original.amount)
        self.assertEqual(reversal.account_id, account.pk)
        active = BankTransaction.objects.filter(company=self.company, account=account, is_void=False)
        income = active.filter(direction=BankTransaction.INCOME).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        expense = active.filter(direction=BankTransaction.EXPENSE).aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
        self.assertEqual(income - expense, Decimal("0.00"))
        cashflow = self.client.get(reverse("cashflow_report"))
        self.assertEqual(cashflow.status_code, 200)
        self.assertEqual(cashflow.context["bank_in"], Decimal("200.00"))
        self.assertEqual(cashflow.context["bank_out"], Decimal("200.00"))
        self.assertEqual(cashflow.context["bank_net"], Decimal("0.00"))
        pnl = self.client.get(reverse("finance_overview"))
        self.assertEqual(pnl.status_code, 200)
        self.assertEqual(pnl.context["bank_in"], Decimal("200.00"))
        self.assertEqual(pnl.context["bank_out"], Decimal("200.00"))
        self.assertEqual(pnl.context["operating_result"], Decimal("0.00"))
        self.assertEqual(pnl.context["revenue"], Decimal("0.00"))
        self.assertEqual(pnl.context["expenses"], Decimal("0.00"))
        controls = self.client.get(reverse("finance_controls"))
        self.assertEqual(controls.context["bank_period_in"], Decimal("200.00"))
        self.assertEqual(controls.context["bank_period_out"], Decimal("200.00"))
        self.assertEqual(controls.context["period_bank_net"], Decimal("0.00"))
        self.assertEqual(controls.context["period_bank_net"], Decimal("0.00"))

        # Both ledger rows are owned by the compensation lifecycle and cannot
        # be independently voided, which preserves the paired audit trail.
        response = self.client.post(
            reverse("bank_transaction_void", args=[original.pk]),
            {"reason": "Окреме скасування"},
        )
        original.refresh_from_db()
        self.assertFalse(original.is_void)
        reversal.refresh_from_db()
        self.assertFalse(reversal.is_void)

    def test_supplier_credit_note_partial_compensation_void_restores_available_amount(self):
        self.client.force_login(self.admin)
        supplier = Supplier.objects.create(company=self.company, name="Credit note lifecycle supplier")
        purchase = create_purchase(
            self.admin, supplier,
            [(self.product, Decimal("1.000"), Decimal("12.00"))],
        )
        returned = SupplierReturn.objects.create(
            company=self.company, purchase_item=purchase.items.get(), supplier=supplier,
            quantity=Decimal("1.000"), unit_cost=Decimal("12.00"),
            compensation_type="credit", reason="Повернення постачальнику",
            completed_at=timezone.now(), created_by=self.admin,
        )

        response = self.client.post(
            reverse("supplier_return_compensate", args=[returned.pk]),
            {"kind": "credit", "amount": "7.00", "note": "Часткова кредит-нота"},
        )
        self.assertEqual(response.status_code, 302)
        compensation = SupplierCompensation.objects.get(supplier_return=returned, is_void=False)
        self.assertEqual(compensation.amount, Decimal("7.00"))
        report = self.client.get(reverse("supplier_balances_report"))
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.context["totals"]["debt"], Decimal("5.00"))
        controls = self.client.get(reverse("finance_controls"))
        self.assertEqual(controls.status_code, 200)
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("5.00"))
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("5.00"))

        # A second credit note cannot exceed the remaining 5.00.
        response = self.client.post(
            reverse("supplier_return_compensate", args=[returned.pk]),
            {"kind": "credit", "amount": "6.00", "note": "Перевищення"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            SupplierCompensation.objects.filter(supplier_return=returned, is_void=False).aggregate(
                total=Sum("amount")
            )["total"],
            Decimal("7.00"),
        )

        response = self.client.post(
            reverse("supplier_compensation_void", args=[compensation.pk]),
            {"reason": "Помилкова кредит-нота"},
        )
        self.assertEqual(response.status_code, 302)
        compensation.refresh_from_db()
        self.assertTrue(compensation.is_void)
        self.assertEqual(compensation.void_reason, "Помилкова кредит-нота")
        self.assertIsNotNone(compensation.voided_at)
        self.assertEqual(
            SupplierCompensation.objects.filter(supplier_return=returned, is_void=False).count(),
            0,
        )
        report = self.client.get(reverse("supplier_balances_report"))
        self.assertEqual(report.context["totals"]["debt"], Decimal("12.00"))
        controls = self.client.get(reverse("finance_controls"))
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("12.00"))
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("12.00"))

        response = self.client.post(
            reverse("supplier_return_compensate", args=[returned.pk]),
            {"kind": "credit", "amount": "12.00", "note": "Повторна правильна кредит-нота"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SupplierCompensation.objects.filter(
            supplier_return=returned, amount=Decimal("12.00"), is_void=False
        ).exists())
        report = self.client.get(reverse("supplier_balances_report"))
        self.assertEqual(report.context["totals"]["debt"], Decimal("0.00"))
        controls = self.client.get(reverse("finance_controls"))
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("0.00"))
        self.assertEqual(controls.context["supplier_debt_total"], Decimal("0.00"))

