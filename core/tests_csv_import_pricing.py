from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.admin_tools import apply_rows
from core.models import Company, MarkupTier, PriceHistory, PriceRule, Product, StaffMembership


class CsvImportSalePriceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("csv-admin", "", "password")
        self.company = Company.objects.create(name="CSV shop", slug="csv-shop", owner=self.admin)
        StaffMembership.objects.create(company=self.company, user=self.admin, role="owner")
        MarkupTier.objects.create(company=self.company, name="Роздріб", percent=30, is_default=True)

    def test_new_product_gets_sale_price_from_cost_when_csv_price_is_empty(self):
        rows = [{
            "sku": "CSV-001", "name": "Тестова деталь", "brand": "BILSTEIN",
            "cost_price": "1000", "new": True,
        }]
        created, updated = apply_rows(rows, self.admin, self.company)
        product = Product.objects.get(company=self.company, sku="CSV-001")
        self.assertEqual((created, updated), (1, 0))
        self.assertEqual(product.cost_price, Decimal("1000"))
        self.assertEqual(product.price, Decimal("1300.00"))

    def test_import_uses_brand_price_rule(self):
        PriceRule.objects.create(company=self.company, name="Bilstein", brand="BILSTEIN", markup_pct=40)
        rows = [{
            "sku": "CSV-002", "name": "Деталь Bilstein", "brand": "BILSTEIN",
            "cost_price": "1000", "new": True,
        }]
        apply_rows(rows, self.admin, self.company)
        self.assertEqual(Product.objects.get(sku="CSV-002").price, Decimal("1400.00"))

    def test_existing_product_recalculates_when_cost_is_imported_without_sale_price(self):
        product = Product.objects.create(
            company=self.company, sku="CSV-003", name="Стара деталь",
            cost_price=Decimal("500"), price=Decimal("650"),
        )
        rows = [{
            "pk": product.pk, "sku": product.sku, "new": False,
            "cost_price": "1000",
        }]
        created, updated = apply_rows(rows, self.admin, self.company)
        product.refresh_from_db()
        self.assertEqual((created, updated), (0, 1))
        self.assertEqual(product.price, Decimal("1300.00"))
        self.assertTrue(PriceHistory.objects.filter(
            company=self.company, product=product, field="price",
            old_value=Decimal("650"), new_value=Decimal("1300.00"),
        ).exists())

    def test_explicit_sale_price_in_csv_is_preserved(self):
        rows = [{
            "sku": "CSV-004", "name": "Ціна задана вручну", "brand": "BILSTEIN",
            "cost_price": "1000", "price": "1500", "new": True,
        }]
        apply_rows(rows, self.admin, self.company)
        self.assertEqual(Product.objects.get(sku="CSV-004").price, Decimal("1500"))
