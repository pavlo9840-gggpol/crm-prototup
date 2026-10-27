from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Product, StockAdjustment, StockMovement


class StaffAccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            username="inventory-manager", password="test-password", is_staff=True
        )
        cls.cashier = User.objects.create_user(
            username="inventory-cashier", password="test-password", is_staff=False
        )
        cls.product = Product.objects.create(
            name="Oil filter",
            brand="FILTERCO",
            sku="OF-ACCESS-01",
            current_stock=Decimal("5.000"),
            price=Decimal("20.00"),
            cost_price=Decimal("12.00"),
        )

    def test_cashier_cannot_open_staff_product_and_reference_data_forms(self):
        self.client.force_login(self.cashier)
        for route in (
            "product_list",
            "product_new",
            "category_list",
            "category_new",
            "supplier_list",
            "supplier_new",
        ):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 403)

    def test_staff_can_open_product_and_reference_data_forms(self):
        self.client.force_login(self.staff)
        for route in (
            "product_list",
            "product_new",
            "category_list",
            "category_new",
            "supplier_list",
            "supplier_new",
        ):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)

    def test_cashier_cannot_change_inventory_even_with_direct_post(self):
        self.client.force_login(self.cashier)
        response = self.client.post(
            reverse("inventory_adjust"),
            {
                "product": self.product.pk,
                "kind": "surplus",
                "quantity": "3.000",
                "reason": "other",
                "note": "direct URL permission test",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("5.000"))
        self.assertEqual(StockAdjustment.objects.count(), 0)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 0)
