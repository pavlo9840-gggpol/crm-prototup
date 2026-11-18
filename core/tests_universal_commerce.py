from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory
from decimal import Decimal
from django.db import IntegrityError, transaction

from core.forms import ProductForm
from core.models import Company, Product, ProductBarcode, StaffMembership
from core.context import layout


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

    def test_automotive_navigation_is_hidden_for_grocery_profile(self):
        User = get_user_model()
        owner = User.objects.create_superuser("grocery-nav-owner", "", "secret")
        store = Company.objects.create(name="Grocery", slug="grocery-nav", owner=owner, business_type="grocery")
        membership = StaffMembership.objects.create(company=store, user=owner, role="owner")
        request = RequestFactory().get("/")
        request.user = owner
        request.company = store
        request.company_access = True
        request.staff_membership = membership
        request.active_license = None
        nav = layout(request)
        group_titles = [group["title"] for group in nav["nav_groups"]]
        self.assertNotIn("Каталог автозапчастин", group_titles)

    def test_automotive_navigation_remains_for_auto_parts_profile(self):
        User = get_user_model()
        owner = User.objects.create_superuser("auto-nav-owner", "", "secret")
        store = Company.objects.create(name="Auto", slug="auto-nav", owner=owner, business_type="auto_parts")
        membership = StaffMembership.objects.create(company=store, user=owner, role="owner")
        request = RequestFactory().get("/")
        request.user = owner
        request.company = store
        request.company_access = True
        request.staff_membership = membership
        request.active_license = None
        nav = layout(request)
        group_titles = [group["title"] for group in nav["nav_groups"]]
        self.assertIn("Каталог автозапчастин", group_titles)

    def test_owner_can_change_store_profile_without_losing_existing_products(self):
        User = get_user_model()
        owner = User.objects.create_superuser("profile-owner", "", "secret")
        store = Company.objects.create(name="Shop", slug="profile-switch-shop", owner=owner)
        StaffMembership.objects.create(company=store, user=owner, role="owner")
        product = Product.objects.create(company=store, name="Тестовий товар", sku="KEEP-001", brand="BILSTEIN")
        self.client.force_login(owner)
        response = self.client.post("/settings/business-profile/", {"business_type": "electronics"})
        self.assertEqual(response.status_code, 302)
        store.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(store.business_type, "electronics")
        self.assertEqual(product.sku, "KEEP-001")
        self.assertEqual(product.brand, "BILSTEIN")
        self.assertEqual(self.client.get("/settings/business-profile/").status_code, 200)

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
        self.owner = owner
        self.client.force_login(owner)

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
        product.refresh_from_db()
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

    def test_product_form_saves_extra_barcodes_and_pos_finds_them_exactly(self):
        form = ProductForm({
            "name": "Чай чорний", "sku": "TEA-002", "barcode": "4820000000200",
            "additional_barcodes": "4820000000201\n4820000000202",
            "unit": "шт", "condition": "new", "cost_price": "20", "price": "30", "min_stock": "0", "is_active": "on",
        }, company=self.company)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save(commit=False)
        product.company = self.company
        product.save()
        form.sync_additional_barcodes(product)
        self.assertEqual(set(product.extra_barcodes.values_list("barcode", flat=True)),
                         {"4820000000201", "4820000000202"})
        response = self.client.get("/pos/search/", {"q": "4820000000202"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["exact"])
        self.assertEqual(response.json()["results"][0]["id"], product.pk)

    def test_extra_barcode_cannot_duplicate_another_products_primary_or_extra_code(self):
        Product.objects.create(company=self.company, name="Чай", sku="TEA-003", barcode="4820000000300")
        form = ProductForm({
            "name": "Інший чай", "sku": "TEA-004", "additional_barcodes": "4820000000300",
            "unit": "шт", "cost_price": "20", "price": "30", "min_stock": "0",
        }, company=self.company)
        self.assertFalse(form.is_valid())
        self.assertIn("additional_barcodes", form.errors)

    def test_barcode_rejects_product_from_another_store(self):
        User = get_user_model()
        other_owner = User.objects.create_superuser("other-owner", "", "secret")
        other = Company.objects.create(name="Інший магазин", slug="other-store", owner=other_owner)
        product = Product.objects.create(company=other, name="Товар", sku="OTHER-01")
        barcode = ProductBarcode(company=self.company, product=product, barcode="4820000000199")
        with self.assertRaises(ValidationError):
            barcode.full_clean()
