from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.context import CASHIER_NAV, STAFF_NAV
from core.tests_tenant_helpers import create_test_company


class UnifiedNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username="manager", password="test-password", is_staff=True)
        cls.cashier = User.objects.create_user(username="cashier", password="test-password")
        cls.company = create_test_company(cls.staff, "Dashboard test shop", [(cls.cashier, "cashier")])

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
        self.assertRedirects(response, reverse("pos"), fetch_redirect_response=False)  <div class="card {% if overdue_orders %}low{% endif %}">Замовлення для клієнтів<b>{{ waiting_orders }}</b>{% if overdue_orders %}<span class="err">Прострочених: {{ overdue_orders }}</span>{% endif %}<a href="{% url 'orders' %}">Відкрити замовлення →</a></div>
</div>
<div class="kpi">
  <div class="card {% if customer_debt %}low{% endif %}">Борг клієнтів<b>{{ customer_debt|floatformat:2 }} ₴</b><a href="{% url 'settlements_home' %}">Контроль розрахунків →</a></div>
  <div class="card {% if supplier_debt %}low{% endif %}">Борг постачальникам<b>{{ supplier_debt|floatformat:2 }} ₴</b><a href="{% url 'settlements_home' %}">Контроль розрахунків →</a></div>
  <div class="card">Невикористані аванси клієнтів<b>{{ customer_advances|floatformat:2 }} ₴</b><a href="{% url 'settlements_home' %}">Аванси →</a></div>
  <div class="card">Аванси постачальникам<b>{{ supplier_advances|floatformat:2 }} ₴</b><a href="{% url 'settlements_home' %}">Аванси →</a></div>
</div>