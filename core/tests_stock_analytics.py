from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Product, Sale, SaleItem
from core.tests_tenant_helpers import create_test_company


class StockAnalyticsReportTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="stock-report-owner", email="", password="secret")
        self.company = create_test_company(self.owner, "Stock analytics", [])
        self.client.force_login(self.owner)
        self.never = Product.objects.create(company=self.company, name="Never sold", sku="NEVER",
            current_stock=Decimal("5"), cost_price=Decimal("10"))
        self.stale = Product.objects.create(company=self.company, name="Stale item", sku="STALE",
            current_stock=Decimal("3"), cost_price=Decimal("20"))
        self.recent = Product.objects.create(company=self.company, name="Recent item", sku="RECENT",
            current_stock=Decimal("2"), cost_price=Decimal("30"))

    def _sale_item(self, product, days_ago):
        sold_at = timezone.now() - timedelta(days=days_ago)
        sale = Sale.objects.create(company=self.company, cashier=self.owner, status=Sale.DONE,
            total=Decimal("10"))
        Sale.objects.filter(pk=sale.pk).update(created_at=sold_at)
        SaleItem.objects.create(company=self.company, sale=sale, product=product, quantity=1,
            original_price=10, price=10, cost_price=product.cost_price)

    def test_report_lists_never_sold_and_30_day_stagnant_stock_and_values(self):
        self._sale_item(self.stale, 45)
        self._sale_item(self.recent, 5)
        response = self.client.get(reverse("report_stock"))
        self.assertEqual(response.status_code, 200)
        rows = {row["product"].sku: row for row in response.context["stagnant"]}
        self.assertIn("NEVER", rows)
        self.assertIn("STALE", rows)
        self.assertNotIn("RECENT", rows)
        self.assertEqual(rows["NEVER"]["status"], "never")
        self.assertEqual(rows["STALE"]["status"], "30+")
        self.assertEqual(response.context["stagnant_value"], Decimal("110.00"))
        self.assertEqual(response.context["stagnant_qty"], Decimal("8.000"))
        self.assertEqual(response.context["product_count"], 3)
        self.assertContains(response, "Залежані запаси")

    def test_report_does_not_include_other_company_products_or_sales(self):
        other_owner = get_user_model().objects.create_superuser(username="stock-report-other", email="", password="secret")
        other_company = create_test_company(other_owner, "Other stock analytics", [])
        Product.objects.create(company=other_company, name="Foreign stock", sku="FOREIGN",
            current_stock=100, cost_price=100)
        response = self.client.get(reverse("report_stock"))
        rows = {row["product"].sku for row in response.context["stagnant"]}
        self.assertNotIn("FOREIGN", rows)
        self.assertEqual(response.context["value"], Decimal("170.00"))
