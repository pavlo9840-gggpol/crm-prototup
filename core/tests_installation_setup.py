from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Company, CompanyMembership


class FirstRunSetupTests(TestCase):
    def test_empty_installation_redirects_to_setup_and_creates_first_owner(self):
        User = get_user_model()
        self.assertFalse(User.objects.exists())
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/setup/").status_code, 200)

        response = self.client.post("/setup/", {
            "shop_name": "АвтоЗапас Тест",
            "username": "first-owner",
            "email": "owner@example.test",
            "password": "V3ry-Strong-Setup-Pass!",
            "password_confirm": "V3ry-Strong-Setup-Pass!",
        })
        self.assertEqual(response.status_code, 302)
        owner = User.objects.get(username="first-owner")
        self.assertTrue(owner.is_superuser)
        store = Company.objects.get()
        self.assertEqual(store.name, "АвтоЗапас Тест")
        self.assertEqual(store.owner, owner)
        membership = CompanyMembership.objects.get(company=store, user=owner)
        self.assertEqual(membership.role, "owner")
        self.assertTrue(membership.is_active)

    def test_setup_cannot_be_reused_after_installation(self):
        User = get_user_model()
        owner = User.objects.create_superuser(username="existing-owner", password="secret")
        store = Company.objects.first()
        store.owner = owner
        store.save(update_fields=["owner"])
        CompanyMembership.objects.create(company=store, user=owner, role="owner")
        self.client.force_login(owner)

        response = self.client.get("/setup/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))

    @override_settings(LICENSE_ENFORCEMENT_ENABLED=True, AUTOZAPAZ_LICENSE_PUBLIC_KEY="")
    def test_production_license_gate_redirects_unlicensed_store_to_activation(self):
        User = get_user_model()
        owner = User.objects.create_superuser(username="license-owner", password="secret")
        store = Company.objects.first()
        store.owner = owner
        store.save(update_fields=["owner"])
        CompanyMembership.objects.create(company=store, user=owner, role="owner")
        self.client.force_login(owner)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/activate/")
        self.assertEqual(self.client.get("/activate/").status_code, 200)
