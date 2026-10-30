from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.context import CASHIER_NAV, STAFF_NAV


class UnifiedNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username="manager", password="test-password", is_staff=True)
        cls.cashier = User.objects.create_user(username="cashier", password="test-password")

    def test_every_staff_navigation_item_resolves(self):
        for _group, items in STAFF_NAV:
            for _label, route_name in items:
                with self.subTest(group=_group, route=route_name):
                    self.assertTrue(reverse(route_name))

    def test_every_cashier_navigation_item_resolves(self):
        for label, route_name in CASHIER_NAV:
            with self.subTest(label=label, route=route_name):
                self.assertTrue(reverse(route_name))

    def test_staff_dashboard_renders_integrated_workspaces(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Операційний центр")
        self.assertContains(response, "Приймання товару")
        self.assertContains(response, "Контроль цін і маржі")
        self.assertContains(response, reverse("receive_home"))
        self.assertContains(response, reverse("supplier_prices"))

    def test_cashier_is_redirected_to_point_of_sale_from_dashboard(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("pos"), fetch_redirect_response=False)
