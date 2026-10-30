from django.contrib.auth import get_user_model
from django.test import TestCase

from core.forms import ProductForm
from core.models import Category, Product


class ProductConditionTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("products-admin", "admin@example.test", "secret")
        self.client.force_login(user)

    def payload(self, **extra):
        data = {"brand": "TEST", "sku": "T-100", "name": "Гальмівні колодки", "condition": "new"}
        data.update(extra)
        return data

    def test_new_is_default_and_used_without_defect_needs_no_description(self):
        form = ProductForm(data={"brand": "TEST", "sku": "T-100", "name": "Деталь"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["condition"], "new")

        form = ProductForm(data=self.payload(condition="used"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_used_defect_requires_description(self):
        form = ProductForm(data=self.payload(condition="used", has_defect="on"))
        self.assertFalse(form.is_valid())
        self.assertIn("condition_description", form.errors)

        form = ProductForm(data=self.payload(condition="used", has_defect="on",
                                             condition_description="Пошкоджений корпус"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_incomplete_requires_missing_parts_description(self):
        form = ProductForm(data=self.payload(condition="incomplete"))
        self.assertFalse(form.is_valid())
        self.assertIn("condition_description", form.errors)

        form = ProductForm(data=self.payload(condition="incomplete",
                                             condition_description="Немає кріплень"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_photo_url_requires_https(self):
        form = ProductForm(data=self.payload(photo_url="http://example.com/photo.jpg"))
        self.assertFalse(form.is_valid())
        self.assertIn("photo_url", form.errors)

        form = ProductForm(data=self.payload(photo_url="https://example.com/photo.jpg"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_category_is_suggested_from_existing_seeded_categories(self):
        category = Category.objects.get(name="Гальмівна система", parent__isnull=True)
        form = ProductForm(data=self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["category"], category)

    def test_product_list_filters_by_condition_and_shows_photo_column(self):
        Product.objects.create(name="Нова деталь", sku="NEW-1", brand="TEST", condition="new")
        Product.objects.create(name="Вживана деталь", sku="USED-1", brand="TEST", condition="used")
        response = self.client.get("/products/?condition=used")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Вживана деталь")
        self.assertNotContains(response, "Нова деталь")
        self.assertContains(response, "Немає фото")

    def test_stock_movements_navigation_does_not_activate_stock_balances(self):
        response = self.client.get("/inventory/movements/")
        self.assertEqual(response.status_code, 200)
        groups = response.context["nav_groups"]
        stock = next(group for group in groups if group["title"] == "Склад")
        active = [item["name"] for item in stock["items"] if item["active"]]
        self.assertEqual(active, ["inventory_movements"])
