import json
from decimal import Decimal
from django.utils import timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import CashOperation, Product, Sale, SaleItem, Shift, SaleReturn
from core.services import StockError, create_return, save_held


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
