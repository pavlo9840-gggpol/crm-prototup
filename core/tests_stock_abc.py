from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Product, Sale, SaleItem
from core.tests_tenant_helpers import create_test_company


class StockABCReportTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="stock-abc-owner", email="", password="secret")
        self.company = create_test_company(self.owner, "ABC shop", [])
        self.client.force_login(self.owner)
        self.a = Product.objects.create(company=self.company, name="A item", sku="A-1",
            current_stock=10, min_stock=2, cost_price=10)
        self.b = Product.objects.create(company=self.company, name="B item", sku="B-1",
            current_stock=5, min_stock=1, cost_price=20)
        self.c = Product.objects.create(company=self.company, name="C item", sku="C-1",
            current_stock=0, min_stock=3, cost_price=5)
        self.unsold = Product.objects.create(company=self.company, name="Unsold", sku="NO-SALES",
            current_stock=4, min_stock=1, cost_price=7)

    def add_sale(self, product, qty, price, returned=0):
        sale = Sale.objects.create(company=self.company, cashier=self.owner, status=Sale.DONE,
            total=Decimal(str(qty)) * Decimal(str(price)))
        SaleItem.objects.create(company=self.company, sale=sale, product=product,
            quantity=qty, returned_qty=returned, original_price=price, price=price,
            cost_price=product.cost_price, source=SaleItem.STOCK)

    def test_abc_classes_net_revenue_stock_coverage_and_replenishment(self):
        self.add_sale(self.a, 8, 100)
        self.add_sale(self.b, 2, 75)
        self.add_sale(self.c, 1, 50)
        response = self.client.get(reverse("report_stock"))
        self.assertEqual(response.status_code, 200)
        rows = {r["product__sku"]: r for r in response.context["abc_rows"]}
        self.assertEqual(rows["A-1"]["abc"], "A")
        self.assertEqual(rows["B-1"]["abc"], "B")
        self.assertEqual(rows["C-1"]["abc"], "C")
        self.assertEqual(rows["NO-SALES"]["abc"], "—")
        self.assertEqual(rows["A-1"]["revenue"], Decimal("800.00"))
        self.assertGreater(rows["A-1"]["coverage_days"], 0)
        self.assertTrue(any(row["product__sku"] == "C-1" for row in response.context["replenishment_rows"]))
        self.assertEqual(response.context["total_revenue"], Decimal("1000.00"))
        self.assertContains(response, "ABC-аналіз")
        self.assertContains(response, "Днів запасу")

    def test_abc_period_filter_excludes_sales_outside_selected_dates(self):
        self.add_sale(self.a, 1, 100)
        response = self.client.get(reverse("report_stock"), {
            "from": (timezone.localdate() - timedelta(days=10)).isoformat(),
            "to": (timezone.localdate() - timedelta(days=5)).isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_revenue"], Decimal("0"))
        self.assertTrue(all(row["abc"] == "—" for row in response.context["abc_rows"]))
