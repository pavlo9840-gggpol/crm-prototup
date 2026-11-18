from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from decimal import Decimal
from django.db import IntegrityError, transaction

from core.forms import ProductForm
from core.models import Company, Product, ProductBarcode, StaffMembership


class UniversalStoreProfileTests(TestCase):
    def test_first_run_can_create_a_grocery_store_profile(self):
        response = self.client.post("/setup/", {
            "business_type": "grocery",
            "shop_name": "Продуктовий магазин",
            "username": "grocery-owner",
            "email": "grocery@example.test",
            "password": "V3ry-Strong-Setup-Pass!",
            "password_confirm": "V3ry-Strong-Setup-Pass!",
        })
        self.assertEqual(response.status_code, 302)
        store = Company.objects.get()
        self.assertEqual(store.business_type, "grocery")
        self.assertEqual(store.name, "Продуктовий магазин")
        self.assertEqual(StaffMembership.objects.get(company=store).role, "owner")

    def test_existing_local_store_defaults_to_automotive_without_data_loss(self):
        User = get_user_model()
        owner = User.objects.create_superuser("owner", "", "secret")
        store = Company.objects.create(name="Existing AutoZapaz", slug="existing-autozapaz", owner=owner)
        product = Product.objects.create(company=store, name="Амортизатор", sku="19-152", brand="BILSTEIN")
        self.assertEqual(store.business_type, "auto_parts")
        self.assertEqual(product.brand, "BILSTEIN")
        self.assertEqual(product.tracking_mode, "none")
        self.assertEqual(product.product_kind, "goods")


class UniversalProductFieldsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        owner = User.objects.create_superuser("owner", "", "secret")
        self.company = Company.objects.create(name="Універсальний магазин", slug="universal-shop", owner=owner)
        StaffMembership.objects.create(company=self.company, user=owner, role="owner")

    def test_product_form_accepts_non_automotive_item_without_brand(self):
        form = ProductForm({
            "name": "Молоко 2.5%",
            "sku": "MILK-001",
            "barcode": "4820000000001",
            "unit": "шт",
            "product_kind": "goods",
            "tracking_mode": "expiry",
            "tax_category": "general",
            "cost_price": "30.00",
            "price": "42.00",
            "min_stock": "5",
            "initial_stock": "12",
            "condition": "new",
        }, company=self.company)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save(commit=False)
        product.company = self.company
        product.save()
        self.assertEqual(product.brand, "")
        self.assertEqual(product.tracking_mode, "expiry")
        self.assertEqual(product.current_stock, 0)  # opening stock is recorded by the view's stock ledger

    def test_retail_profile_requires_unique_internal_sku(self):
        self.company.business_type = "grocery"
        self.company.save(update_fields=["business_type"])
        Product.objects.create(company=self.company, name="Молоко 1 л", sku="MILK-001")
        form = ProductForm({
            "name": "Молоко 2.5%", "sku": "MILK 001", "unit": "шт",
            "product_kind": "goods", "tracking_mode": "expiry", "tax_category": "general",
            "cost_price": "30", "price": "42", "min_stock": "0", "condition": "new",
        }, company=self.company)
        self.assertFalse(form.is_valid())
        self.assertIn("sku", form.errors)

    def test_auto_parts_profile_allows_same_sku_for_different_brands(self):
        Product.objects.create(company=self.company, name="Амортизатор Bilstein", sku="19-152", brand="BILSTEIN")
        form = ProductForm({
            "name": "Амортизатор Sachs", "sku": "19152", "brand": "SACHS", "unit": "шт",
            "product_kind": "goods", "tracking_mode": "none", "tax_category": "general",
            "cost_price": "100", "price": "130", "min_stock": "0", "condition": "new",
        }, company=self.company)
        self.assertTrue(form.is_valid(), form.errors)

    def test_product_supports_service_serial_and_shipping_attributes(self):
        product = Product.objects.create(
            company=self.company, name="Налаштування обладнання", sku="SERVICE-01",
            product_kind="service", tracking_mode="serial", tax_category="vat_standard",
            weight_kg="1.250", length_cm="10.00", width_cm="20.00", height_cm="30.00",
            attributes={"duration_minutes": 60, "skill": "installation"},
        )
        self.assertEqual(product.product_kind, "service")
        self.assertEqual(product.weight_kg, Decimal("1.250"))
        self.assertEqual(product.attributes["duration_minutes"], 60)

    def test_multiple_barcodes_are_scoped_to_store_and_product(self):
        product = Product.objects.create(company=self.company, name="Чай", sku="TEA-01")
        barcode = ProductBarcode.objects.create(product=product, barcode="4820000000100")
        self.assertEqual(barcode.company_id, self.company.pk)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductBarcode.objects.create(product=product, company=self.company, barcode="4820000000100")

    def test_barcode_rejects_product_from_another_store(self):
        User = get_user_model()
        other_owner = User.objects.create_superuser("other-owner", "", "secret")
        other = Company.objects.create(name="Інший магазин", slug="other-store", owner=other_owner)
        product = Product.objects.create(company=other, name="Товар", sku="OTHER-01")
        barcode = ProductBarcode(company=self.company, product=product, barcode="4820000000199")
        with self.assertRaises(ValidationError):
            barcode.full_clean()
