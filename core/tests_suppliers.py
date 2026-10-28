from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Supplier


class SupplierDirectoryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(username="supplier-manager", password="test-pass", is_staff=True)
        self.active = Supplier.objects.create(name="Auto Parts West", contact_person="Ірина", phone="+380501234567")
        self.inactive = Supplier.objects.create(name="Old Supplier", contact_person="Петро", is_active=False)

    def test_supplier_search_and_active_filter(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("supplier_list"), {"q": "Ірина"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Auto Parts West")
        self.assertNotContains(response, "Old Supplier")
        response = self.client.get(reverse("supplier_list"), {"active": "1"})
        self.assertContains(response, "Auto Parts West")
        self.assertNotContains(response, "Old Supplier")
        response = self.client.get(reverse("supplier_list"), {"active": "0"})
        self.assertContains(response, "Old Supplier")
        self.assertNotContains(response, "Auto Parts West")

    def test_supplier_directory_exposes_api_setup(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("supplier_list"))
        self.assertContains(response, reverse("supplier_api", args=[self.active.pk]))
