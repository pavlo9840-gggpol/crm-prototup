from datetime import timedelta
import json

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.models import (Category, Company, CompanyMembership, CompanySettings, Product, StockMovement,
                         Supplier, SupplierItem, Customer, Sale, Shift, SaleItem)
from core.tenant_context import CompanyContextMiddleware, company_queryset


class CompanyContextTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="workspace-user", password="secret")
        self.other_user = User.objects.create_user(username="other-workspace-user", password="secret")
        self.company = Company.objects.create(name="Магазин А", slug="shop-a", owner=self.user, status="active")
        self.other_company = Company.objects.create(name="Магазин Б", slug="shop-b", owner=self.other_user, status="active")
        CompanyMembership.objects.create(company=self.company, user=self.user, role="owner")
        CompanyMembership.objects.create(company=self.other_company, user=self.other_user, role="owner")
        self.own_product = Product.objects.create(name="Свій товар", sku="OWN-1", company=self.company)
        self.other_product = Product.objects.create(name="Чужий товар", sku="OTHER-1", company=self.other_company)
        self.factory = RequestFactory()
        self.middleware = CompanyContextMiddleware(lambda request: request)

    def request_for(self, user, session=None):
        request = self.factory.get("/")
        request.user = user
        request.session = session if session is not None else {}
        return self.middleware(request)

    def test_single_membership_is_selected_and_queryset_is_scoped(self):
        request = self.request_for(self.user)

        self.assertEqual(request.company, self.company)
        self.assertTrue(request.company_access)
        self.assertEqual(request.session["active_company_id"], self.company.pk)
        rows = company_queryset(Product.objects.all(), request)
        self.assertQuerySetEqual(rows.order_by("pk"), [self.own_product], transform=lambda row: row)

    def test_no_membership_means_no_company_and_no_rows(self):
        request = self.request_for(get_user_model().objects.create_user(username="unassigned"))

        self.assertIsNone(request.company)
        self.assertFalse(company_queryset(Product.objects.all(), request).exists())

    def test_expired_trial_has_no_query_access(self):
        self.company.status = "trial"
        self.company.trial_ends_at = timezone.now() - timedelta(seconds=1)
        self.company.save(update_fields=["status", "trial_ends_at"])
        request = self.request_for(self.user, {"active_company_id": self.company.pk})

        self.assertIsNotNone(request.company)
        self.assertFalse(request.company_access)
        self.assertFalse(company_queryset(Product.objects.all(), request).exists())

    def test_multiple_memberships_require_explicit_selection(self):
        CompanyMembership.objects.create(company=self.other_company, user=self.user, role="manager")
        request = self.request_for(self.user)

        self.assertIsNone(request.company)
        selected = self.request_for(self.user, {"active_company_id": self.other_company.pk})
        self.assertEqual(selected.company, self.other_company)

    def test_company_settings_are_separate_per_workspace(self):
        own_settings = CompanySettings.get(self.company)
        other_settings = CompanySettings.get(self.other_company)
        own_settings.name = "Налаштування магазину А"
        own_settings.save(update_fields=["name"])

        self.assertEqual(CompanySettings.get(self.company).name, "Налаштування магазину А")
        self.assertEqual(CompanySettings.get(self.other_company).name, self.other_company.name)
        self.assertNotEqual(own_settings.pk, other_settings.pk)

    def test_company_switch_requires_membership_and_sets_session(self):
        CompanyMembership.objects.create(company=self.other_company, user=self.user, role="manager")
        self.client.force_login(self.user)

        response = self.client.post("/workspace/select/", {"company_id": self.other_company.pk})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["active_company_id"], self.other_company.pk)

    def test_company_switch_rejects_non_member(self):
        self.client.force_login(self.user)

        response = self.client.post("/workspace/select/", {"company_id": self.other_company.pk})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.session["active_company_id"], self.company.pk)

    def test_workspace_onboarding_creates_owner_membership_and_selects_workspace(self):
        self.client.force_login(self.other_user)

        response = self.client.post("/workspace/new/", {"name": "Новий магазин"})

        self.assertEqual(response.status_code, 302)
        company = Company.objects.get(name="Новий магазин")
        membership = CompanyMembership.objects.get(company=company, user=self.other_user)
        self.assertEqual(membership.role, "owner")
        self.assertEqual(self.client.session["active_company_id"], company.pk)
        self.assertTrue(company.has_access)

    def test_pos_sales_and_cash_records_cannot_cross_company_boundaries(self):
        own_customer = Customer.objects.create(company=self.company, name="Клієнт А", phone="380111111")
        foreign_customer = Customer.objects.create(company=self.other_company, name="Клієнт Б", phone="380222222")
        foreign_shift = Shift.objects.create(company=self.other_company, cashier=self.other_user)
        foreign_sale = Sale.objects.create(company=self.other_company, cashier=self.other_user, shift=foreign_shift,
                                           status=Sale.DONE, number="FOREIGN-CH", total=500)
        foreign_product = Product.objects.create(company=self.other_company, name="Чужа помпа", sku="FOREIGN-PUMP",
                                                  current_stock=10, price=500)
        SaleItem.objects.create(company=self.other_company, sale=foreign_sale, product=foreign_product,
                                quantity=1, original_price=500, price=500)

        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/pos/search/?q=FOREIGN-PUMP").json()["results"], [])
        self.assertEqual(self.client.get("/pos/customer/?q=Клієнт Б").json()["results"], [])
        self.assertEqual(self.client.get(f"/sales/{foreign_sale.pk}/").status_code, 404)
        sales = self.client.get("/sales/")
        self.assertNotContains(sales, "FOREIGN-CH")

        self.client.post("/shift/", {"action": "open", "cash": "0"})
        response = self.client.post(
            "/pos/checkout/",
            data=json.dumps({"items": [{"id": foreign_product.pk, "qty": "1"}],
                             "payment": "card", "card": "500"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Sale.objects.filter(company=self.company, status=Sale.DONE).exists())

        own_sale = Sale.objects.create(company=self.company, cashier=self.user, status=Sale.DONE,
                                       number="OWN-CH", total=10, customer=own_customer)
        self.assertEqual(self.client.get(f"/sales/{own_sale.pk}/").status_code, 200)

    def test_product_catalog_and_detail_are_isolated_between_workspaces(self):
        self.client.force_login(self.user)

        response = self.client.get("/products/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Свій товар")
        self.assertNotContains(response, "Чужий товар")

        response = self.client.get(f"/products/{self.other_product.pk}/edit/")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            f"/products/{self.other_product.pk}/edit/",
            {"brand": "HACK", "sku": "HACK-1", "name": "Зміна чужого товару",
             "unit": "шт", "cost_price": "1", "price": "2", "min_stock": "0"},
        )
        self.assertEqual(response.status_code, 404)
        self.other_product.refresh_from_db()
        self.assertEqual(self.other_product.name, "Чужий товар")

    def test_product_cannot_attach_foreign_category_and_new_product_gets_company(self):
        foreign_category = Category.objects.create(company=self.other_company, name="Чужа категорія")
        self.client.force_login(self.user)

        response = self.client.post(
            "/products/new/",
            {"brand": "BOSCH", "sku": "TENANT-1", "name": "Новий фільтр",
             "category": foreign_category.pk, "unit": "шт", "cost_price": "100",
             "price": "130", "min_stock": "0", "initial_stock": "2", "condition": "new", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)  # invalid ModelChoice is rendered, not saved
        self.assertFalse(Product.objects.filter(sku="TENANT-1").exists())

        response = self.client.post(
            "/products/new/",
            {"brand": "BOSCH", "sku": "TENANT-2", "name": "Свій фільтр",
             "unit": "шт", "cost_price": "100", "price": "130",
             "min_stock": "0", "initial_stock": "2", "condition": "new", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        created = Product.objects.get(sku="TENANT-2")
        self.assertEqual(created.company, self.company)
        movement = StockMovement.objects.get(product=created)
        self.assertEqual(movement.company, self.company)

    def test_category_and_supplier_forms_reject_foreign_objects_and_set_company(self):
        foreign_category = Category.objects.create(company=self.other_company, name="Зовнішня")
        foreign_supplier = Supplier.objects.create(company=self.other_company, name="Чужий постачальник")
        SupplierItem.objects.create(company=self.other_company, supplier=foreign_supplier, brand="FOREIGN",
                                     sku="FOREIGN-42", sku_norm="FOREIGN42", cost=99)
        self.client.force_login(self.user)

        categories = self.client.get("/categories/")
        suppliers = self.client.get("/suppliers/")
        self.assertNotContains(categories, "Зовнішня")
        self.assertNotContains(suppliers, "Чужий постачальник")
        lookup = self.client.get("/products/lookup/", {"b": "FOREIGN", "s": "FOREIGN-42"}).json()
        self.assertEqual(lookup["offers"], [])

        response = self.client.post("/categories/new/", {"name": "Дочірня", "parent": foreign_category.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Category.objects.filter(name="Дочірня").exists())

        response = self.client.post(f"/suppliers/{foreign_supplier.pk}/edit/", {"name": "Підміна чужого"})
        self.assertEqual(response.status_code, 404)
        foreign_supplier.refresh_from_db()
        self.assertEqual(foreign_supplier.name, "Чужий постачальник")

        response = self.client.post("/suppliers/new/", {"name": "Свій постачальник", "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        supplier = Supplier.objects.get(name="Свій постачальник")
        self.assertEqual(supplier.company, self.company)

