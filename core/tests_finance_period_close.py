from datetime import date
from decimal import Decimal
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Company, FinanceEntry, FinancePeriodClose, Product, Sale, SaleItem, StaffMembership


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
        self.assertTrue(close.verify_checksum())
        close.snapshot["metrics"]["cash_net_movement"] = "999999.00"
        self.assertFalse(close.verify_checksum())

    def test_period_close_cannot_be_overwritten_by_a_second_submission(self):
        self._close()
        close = FinancePeriodClose.objects.get(company=self.company)
        original_snapshot = close.snapshot
        response = self._close()
        self.assertEqual(response.status_code, 302)
        close.refresh_from_db()
        self.assertEqual(close.snapshot, original_snapshot)
        self.assertEqual(FinancePeriodClose.objects.filter(company=self.company).count(), 1)

    def test_period_close_page_detects_checksum_mismatch(self):
        self._close()
        close = FinancePeriodClose.objects.get(company=self.company)
        FinancePeriodClose.objects.filter(pk=close.pk).update(snapshot={"tampered": True})
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["period_close_integrity"])
        self.assertContains(response, "контрольна сума не збігається")

    def test_period_close_snapshot_includes_period_pnl_and_excludes_voided_entries(self):
        from django.utils import timezone

        product = Product.objects.create(
            company=self.company, name="Snapshot item", sku="SNAP-001",
            price=Decimal("50.00"), cost_price=Decimal("30.00"),
            current_stock=Decimal("4.000"),
        )
        sale = Sale.objects.create(
            company=self.company, number="SNAP-SALE-001", status=Sale.DONE,
            cashier=self.user, total=Decimal("50.00"),
        )
        SaleItem.objects.create(
            company=self.company, sale=sale, product=product,
            quantity=Decimal("1.000"), original_price=Decimal("50.00"),
            price=Decimal("50.00"), cost_price=Decimal("30.00"),
            earned_at=timezone.now(),
        )
        FinanceEntry.objects.create(
            company=self.company, entry_type=FinanceEntry.INCOME,
            category="Other income", amount=Decimal("2.00"), created_by=self.user,
        )
        FinanceEntry.objects.create(
            company=self.company, entry_type=FinanceEntry.EXPENSE,
            category="Operating expense", amount=Decimal("7.00"), created_by=self.user,
        )
        voided = FinanceEntry.objects.create(
            company=self.company, entry_type=FinanceEntry.EXPENSE,
            category="Voided expense", amount=Decimal("100.00"), created_by=self.user,
            is_void=True,
        )

        self._close()
        snapshot = FinancePeriodClose.objects.get(company=self.company).snapshot
        metrics = snapshot["metrics"]
        self.assertEqual(metrics["gross_revenue"], "50.00")
        self.assertEqual(metrics["net_revenue"], "50.00")
        self.assertEqual(metrics["gross_cost_of_goods_sold"], "30.00")
        self.assertEqual(metrics["gross_profit"], "20.00")
        self.assertEqual(metrics["other_income"], "2.00")
        self.assertEqual(metrics["operating_expenses"], "7.00")
        self.assertEqual(metrics["operating_result"], "15.00")
        self.assertEqual(metrics["sold_units"], "1.000")
        self.assertEqual(voided.is_void, True)

    def test_period_close_model_rejects_mutation_and_deletion(self):
        self._close()
        close = FinancePeriodClose.objects.get(company=self.company)
        close.snapshot = {"tampered": True}
        with self.assertRaises(ValidationError):
            close.save()
        with self.assertRaises(ValidationError):
            close.delete()
