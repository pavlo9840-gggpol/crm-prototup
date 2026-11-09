from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.models import Company, CompanyMembership, CompanySettings, Product
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
        self.assertNotIn("active_company_id", self.client.session)
