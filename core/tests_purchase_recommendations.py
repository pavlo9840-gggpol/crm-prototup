from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Product, Sale, SaleItem, Supplier, SupplierItem
from core.tests_tenant_helpers import create_test_company


class PurchaseRecommendationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="purchase-recommend-owner", email="", password="secret")
        self.company = create_test_company(self.owner, "Purchase recommendations", [])
        self.client.force_login(self.owner)
        self.product = Product.objects.create(
            company=self.company, name="Filter", sku="FILTER-01", brand="ACME",
            current_stock=Decimal("1"), min_stock=Decimal("2"), cost_price=Decimal("12"),
        )

    def add_sale(self, quantity, days_ago=10):
        sale = Sale.objects.create(company=self.company, cashier=self.owner, status=Sale.DONE, total=Decimal("90"))
        Sale.objects.filter(pk=sale.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        SaleItem.objects.create(
            company=self.company, sale=sale, product=self.product, quantity=Decimal(quantity),
            original_price=Decimal("10"), price=Decimal("10"), cost_price=Decimal("5"),
        )

    def test_recommendation_uses_period_velocity_and_supplier_availability(self):
        self.add_sale("9")
        supplier = Supplier.objects.create(company=self.company, name="Cheapest supplier")
        SupplierItem.objects.create(
            company=self.company, supplier=supplier, product=self.product, brand="ACME",
            sku=self.product.sku, sku_norm=self.product.sku_norm, cost=Decimal("8"),
            qty_avail=Decimal("2"), available=True,
        )
        response = self.client.get(reverse("purchase_recommendations"))
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.context["rows"] if item["product"].pk == self.product.pk)
        # 9 units / 90 days * 30 target days + min 2 - current 1 = 4.
        self.assertEqual(row["recommended"], Decimal("4.000"))
        self.assertEqual(row["supplier"], supplier)
        self.assertEqual(row["supplier_qty"], Decimal("2"))
        self.assertEqual(row["unit_cost"], Decimal("8"))
        self.assertContains(response, "У накладну")

    def test_minimum_stock_generates_recommendation_without_sales(self):
        self.product.current_stock = Decimal("0")
        self.product.min_stock = Decimal("5")
        self.product.save()
        response = self.client.get(reverse("purchase_recommendations"))
        row = next(item for item in response.context["rows"] if item["product"].pk == self.product.pk)
        self.assertEqual(row["recommended"], Decimal("5.000"))
        self.assertIsNone(row["supplier"])

    def test_recommendations_are_company_scoped(self):
        other_owner = get_user_model().objects.create_superuser(username="purchase-recommend-other", email="", password="secret")
        other_company = create_test_company(other_owner, "Other purchase recommendations", [])
        foreign = Product.objects.create(
            company=other_company, name="Foreign", sku="FOREIGN-01",
            current_stock=0, min_stock=10, cost_price=Decimal("1"),
        )
        response = self.client.get(reverse("purchase_recommendations"))
        ids = {item["product"].pk for item in response.context["rows"]}
        self.assertNotIn(foreign.pk, ids)

    def test_recommendation_prefills_purchase_document(self):
        response = self.client.get(reverse("purchase_new"), {
            "product": str(self.product.pk), "qty": "3.000", "cost": "8.50",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_lines"], [{
            "product_id": self.product.pk, "quantity": "3.000", "cost": "8.50",
        }])
