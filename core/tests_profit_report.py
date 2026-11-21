from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Category, Product, Sale, SaleItem
from core.tests_tenant_helpers import create_test_company


class ProfitabilityReportTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="profit-owner", email="", password="secret")
        self.company = create_test_company(self.owner, "Profitability shop", [])
        self.category_a = Category.objects.create(company=self.company, name="Напої")
        self.category_b = Category.objects.create(company=self.company, name="Снеки")
        self.product_a = Product.objects.create(
            company=self.company, name="Сік", sku="JUICE-1", category=self.category_a,
            price=Decimal("100.00"), cost_price=Decimal("60.00"),
        )
        self.product_b = Product.objects.create(
            company=self.company, name="Чипси", sku="CHIPS-1", category=self.category_b,
            price=Decimal("100.00"), cost_price=Decimal("95.00"),
        )
        self.client.force_login(self.owner)

    def _sale_item(self, product, quantity, price, cost, returned=0, earned_at=None):
        sale = Sale.objects.create(
            company=self.company, cashier=self.owner, status=Sale.DONE,
            total=Decimal(str(quantity)) * Decimal(str(price)),
        )
        return SaleItem.objects.create(
            company=self.company, sale=sale, product=product,
            quantity=Decimal(str(quantity)), original_price=Decimal(str(price)),
            price=Decimal(str(price)), cost_price=Decimal(str(cost)),
            returned_qty=Decimal(str(returned)), earned_at=earned_at or timezone.now(),
        )

    def test_report_groups_margin_by_category_and_lists_low_margin_products(self):
        self._sale_item(self.product_a, 2, 100, 60, returned=1)
        self._sale_item(self.product_b, 2, 100, 95)
        response = self.client.get(reverse("report_profit"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rev"], Decimal("300.00"))
        self.assertEqual(response.context["cost"], Decimal("250.00"))
        self.assertEqual(response.context["margin"], Decimal("50.00"))
        categories = {row["product__category__name"]: row for row in response.context["category_rows"]}
        self.assertEqual(categories["Напої"]["margin"], Decimal("40.00"))
        self.assertEqual(categories["Снеки"]["margin"], Decimal("10.00"))
        self.assertEqual(response.context["low_margin_rows"][0]["product__sku"], "CHIPS-1")
        self.assertContains(response, "Рентабельність за категоріями")
        self.assertContains(response, "Товари з найнижчою маржею")

    def test_report_excludes_items_not_yet_earned_and_other_companies(self):
        self._sale_item(self.product_a, 1, 100, 60)
        self._sale_item(self.product_b, 1, 100, 95, earned_at=None)
        pending = SaleItem.objects.filter(product=self.product_b).latest("pk")
        pending.earned_at = None
        pending.save(update_fields=["earned_at"])

        other_owner = get_user_model().objects.create_superuser(
            username="other-profit-owner", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other profitability shop", [])
        other_product = Product.objects.create(company=other_company, name="Інший", sku="OTHER")
        other_sale = Sale.objects.create(company=other_company, cashier=other_owner, status=Sale.DONE)
        SaleItem.objects.create(
            company=other_company, sale=other_sale, product=other_product,
            quantity=1, original_price=999, price=999, cost_price=1, earned_at=timezone.now(),
        )

        response = self.client.get(reverse("report_profit"))
        self.assertEqual(response.context["rev"], Decimal("100.00"))
        self.assertEqual(len(response.context["rows"]), 1)
