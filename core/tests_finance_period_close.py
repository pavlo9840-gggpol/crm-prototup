from datetime import date
from decimal import Decimal
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Company, FinancePeriodClose, StaffMembership


class FinancePeriodCloseTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("periodcloser", "", "test-password")
        self.company = Company.objects.create(name="Period close test", slug="period-close-test", owner=self.user)
        StaffMembership.objects.create(company=self.company, user=self.user, role="owner")
        self.client.login(username="periodcloser", password="test-password")
        self.period_from = date(2026, 11, 1)
        self.period_to = date(2026, 11, 30)
        self.url = "/reports/finance/controls/?from=2026-11-01&to=2026-11-30"

    def _close(self):
        return self.client.post(self.url, {
            "action": "close_period", "from": "2026-11-01", "to": "2026-11-30",
            "confirm_snapshot": "1",
        })

    def test_period_close_requires_explicit_confirmation(self):
        response = self.client.post(self.url, {"action": "close_period"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(FinancePeriodClose.objects.filter(company=self.company).exists())

    def test_period_close_stores_checksum_protected_snapshot(self):
        response = self._close()
        self.assertEqual(response.status_code, 302)
        close = FinancePeriodClose.objects.get(company=self.company)
        canonical = json.dumps(close.snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(close.checksum, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(close.snapshot["period"], {"from": "2026-11-01", "to": "2026-11-30"})
        self.assertEqual(close.snapshot["balance_basis"], "current_at_close")
        self.assertIn("created_at", close.snapshot)
        self.assertEqual(close.closed_by, self.user)

    def test_period_close_cannot_be_overwritten_by_a_second_submission(self):
        self._close()
        close = FinancePeriodClose.objects.get(company=self.company)
        original_snapshot = close.snapshot
        response = self._close()
        self.assertEqual(response.status_code, 302)
        close.refresh_from_db()
        self.assertEqual(close.snapshot, original_snapshot)
        self.assertEqual(FinancePeriodClose.objects.filter(company=self.company).count(), 1)

    def test_period_close_model_rejects_mutation_and_deletion(self):
        self._close()
        close = FinancePeriodClose.objects.get(company=self.company)
        close.snapshot = {"tampered": True}
        with self.assertRaises(ValidationError):
            close.save()
        with self.assertRaises(ValidationError):
            close.delete()
