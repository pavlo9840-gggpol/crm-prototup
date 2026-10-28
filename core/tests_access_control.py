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

    def test_cashier_cannot_import_supplier_prices_or_edit_cross_references(self):
        self.client.force_login(self.cashier)
        protected_gets = ("supplier_prices", "crossrefs", "markup_list")
        for route in protected_gets:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 403)

        # Direct POSTs must be denied before parsing files or applying price changes.
        for route in ("price_map", "price_apply"):
            with self.subTest(route=route):
                self.assertEqual(self.client.post(reverse(route), {"supplier": "1"}).status_code, 403)

        self.assertEqual(
            self.client.post(reverse("crossrefs"), {"act": "add", "a": "A-1", "b": "B-1"}).status_code,
            403,
        )

    def test_only_superuser_can_access_user_and_backup_management(self):
        User = get_user_model()
        root = User.objects.create_superuser(
            username="system-root",
            email="root@example.invalid",
            password="test-password",
        )
        protected_routes = ("user_list", "user_new", "backup")

        for user in (self.cashier, self.staff):
            self.client.force_login(user)
            for route in protected_routes:
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(reverse(route)).status_code, 403)

        self.client.force_login(root)
        for route in protected_routes:
            with self.subTest(user=root.username, route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 200)

    def test_staff_cannot_create_users_or_trigger_backup_by_direct_post(self):
        User = get_user_model()
        before = User.objects.count()
        self.client.force_login(self.staff)

        user_response = self.client.post(
            reverse("user_new"),
            {
                "username": "unauthorized-new-user",
                "first_name": "No",
                "role": "admin",
                "is_active": "on",
                "password": "test-password",
            },
        )
        backup_response = self.client.post(reverse("backup"), {"action": "save"})

        self.assertEqual(user_response.status_code, 403)
        self.assertEqual(backup_response.status_code, 403)
        self.assertEqual(User.objects.count(), before)

    def test_cashier_cannot_access_finance_pricing_payroll_or_stock_reports(self):
        self.client.force_login(self.cashier)
        protected_routes = (
            "purchase_list",
            "purchase_new",
            "price_rules",
            "price_rule_new",
            "price_review",
            "rates",
            "payroll",
            "pay_plans",
            "pay_plan_new",
            "inventory",
            "inventory_movements",
            "report_daily",
            "report_monthly",
            "report_top",
            "report_profit",
            "report_stock",
            "settings_home",
            "company_settings",
            "adjustment_list",
            "designer_home",
            "product_export",
            "product_import",
            "print_stock_report",
            "print_movement_report",
        )
        for route in protected_routes:
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 403)

        # Mutation endpoints must reject direct requests too, without creating rules.
        from core.models import PriceRule
        before = PriceRule.objects.count()
        response = self.client.post(
            reverse("price_rule_new"),
            {"name": "Unauthorized rule", "brand": "FILTERCO", "percent": "99"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PriceRule.objects.count(), before)

