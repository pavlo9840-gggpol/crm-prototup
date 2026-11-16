import base64
import json
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from core.licensing import LicenseError, verify_license_token


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class OfflineLicenseValidationTests(SimpleTestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_key = _b64(public_bytes)

    def make_token(self, **updates):
        now = timezone.now()
        claims = {
            "version": 1,
            "license_id": "test-license-001",
            "plan": "pro",
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "max_users": 5,
            "max_workstations": 3,
            "features": ["supplier_api", "vehicle_catalogue"],
        }
        claims.update(updates)
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "AZ1." + _b64(payload) + "." + _b64(self.private_key.sign(payload))

    @override_settings()
    def test_valid_offline_key_returns_verified_claims(self):
        with self.settings(AUTOZAPAZ_LICENSE_PUBLIC_KEY=self.public_key):
            claims = verify_license_token(self.make_token())
        self.assertEqual(claims["license_id"], "test-license-001")
        self.assertEqual(claims["plan"], "pro")
        self.assertEqual(claims["max_users"], 5)
        self.assertEqual(claims["features"], ["supplier_api", "vehicle_catalogue"])

    def test_modified_payload_is_rejected(self):
        token = self.make_token().split(".")
        token[1] = _b64(b'{"version":1,"license_id":"forged","plan":"business"}')
        with self.settings(AUTOZAPAZ_LICENSE_PUBLIC_KEY=self.public_key):
            with self.assertRaises(LicenseError):
                verify_license_token(".".join(token))

    def test_expired_key_is_rejected(self):
        expired = (timezone.now() - timedelta(days=1)).isoformat()
        with self.settings(AUTOZAPAZ_LICENSE_PUBLIC_KEY=self.public_key):
            with self.assertRaises(LicenseError):
                verify_license_token(self.make_token(expires_at=expired))

    def test_unknown_plan_and_invalid_limits_are_rejected(self):
        with self.settings(AUTOZAPAZ_LICENSE_PUBLIC_KEY=self.public_key):
            with self.assertRaises(LicenseError):
                verify_license_token(self.make_token(plan="enterprise"))
            with self.assertRaises(LicenseError):
                verify_license_token(self.make_token(max_users=0))

    def test_missing_publisher_public_key_fails_closed(self):
        with self.settings(AUTOZAPAZ_LICENSE_PUBLIC_KEY=""):
            with self.assertRaises(LicenseError):
                verify_license_token(self.make_token())
