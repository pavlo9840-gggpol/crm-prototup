import base64
import json
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core.models import (
    Company,
    CompanyMembership,
    InstallationIdentity,
    LicenseActivation,
    LicenseWorkstationSession,
)


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@override_settings(LICENSE_ENFORCEMENT_ENABLED=True)
class LocalLicenseEnforcementTests(TestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self._settings_override = self.settings(AUTOZAPAZ_LICENSE_PUBLIC_KEY=_b64(public_bytes))
        self._settings_override.enable()

        User = get_user_model()
        self.user = User.objects.create_superuser(
            username="license-owner", email="owner@example.test", password="Strong-Password-123"
        )
        self.company = Company.objects.create(
            name="Тестовий магазин", slug="license-test-store", owner=self.user
        )
        CompanyMembership.objects.create(company=self.company, user=self.user, role="owner")
        self.installation_id = uuid.uuid4()
        InstallationIdentity.objects.create(pk=1, installation_id=self.installation_id)
        self.activate("start", max_users=2, max_workstations=1)

    def tearDown(self):
        self._settings_override.disable()

    def make_token(self, plan="start", max_users=2, max_workstations=1, features=None):
        now = timezone.now()
        claims = {
            "version": 1,
            "license_id": "license-enforcement-test",
            "installation_id": str(self.installation_id),
            "customer": "Test shop",
            "plan": plan,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "max_users": max_users,
            "max_workstations": max_workstations,
            "features": list(features or []),
        }
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "AZ1." + _b64(payload) + "." + _b64(self.private_key.sign(payload))

    def activate(self, plan, max_users, max_workstations, features=None):
        token = self.make_token(plan, max_users, max_workstations, features)
        LicenseActivation.objects.update_or_create(
            pk=1,
            defaults={
                "token": token,
                "license_id": "license-enforcement-test",
                "plan": plan,
                "issued_at": timezone.now(),
                "expires_at": timezone.now() + timedelta(days=30),
                "max_users": max_users,
                "max_workstations": max_workstations,
                "features": list(features or []),
            },
        )

    def test_start_plan_blocks_pro_supplier_prices_route(self):
        client = Client()
        client.force_login(self.user)
        response = client.get("/supplier-prices/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Прайси постачальників", status_code=403)

    def test_pro_plan_allows_pro_route_but_blocks_business_service_module(self):
        self.activate("pro", max_users=5, max_workstations=3)
        client = Client()
        client.force_login(self.user)

        supplier_prices = client.get("/supplier-prices/")
        self.assertNotEqual(supplier_prices.status_code, 403)

        service_module = client.get("/service/")
        self.assertEqual(service_module.status_code, 403)
        self.assertContains(service_module, "Модуль СТО", status_code=403)

    def test_signed_addon_can_enable_one_pro_feature_on_start(self):
        self.activate("start", max_users=2, max_workstations=1, features=["supplier_prices"])
        client = Client()
        client.force_login(self.user)
        response = client.get("/supplier-prices/")
        self.assertNotEqual(response.status_code, 403)

    def test_concurrent_workstation_limit_and_logout_release(self):
        first = Client()
        second = Client()
        first.force_login(self.user)
        second.force_login(self.user)

        first_response = first.get("/users/")
        self.assertNotEqual(first_response.status_code, 403)
        self.assertEqual(LicenseWorkstationSession.objects.count(), 1)

        blocked = second.get("/users/")
        self.assertEqual(blocked.status_code, 403)
        self.assertContains(blocked, "дозволено одночасно", status_code=403)

        first.post("/logout/")
        allowed_after_logout = second.get("/users/")
        self.assertNotEqual(allowed_after_logout.status_code, 403)
        self.assertEqual(LicenseWorkstationSession.objects.count(), 1)

    def test_idle_workstation_is_released_after_timeout(self):
        first = Client()
        second = Client()
        first.force_login(self.user)
        second.force_login(self.user)
        first.get("/users/")
        LicenseWorkstationSession.objects.update(
            last_seen=timezone.now() - timedelta(minutes=16)
        )
        response = second.get("/users/")
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(LicenseWorkstationSession.objects.count(), 1)
