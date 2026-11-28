from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import BankAccount, BankTransaction, Company, StaffMembership


class BankOpeningBalanceTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("bankopening", "", "test-password")
        self.company = Company.objects.create(name="Opening balance test", slug="opening-balance-test", owner=self.user)
        StaffMembership.objects.create(company=self.company, user=self.user, role="owner")
        self.client.login(username="bankopening", password="test-password")
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test bank", iban="UA123456789012345678901234567",
            opening_balance=Decimal("1000.00"), opening_balance_date=date(2026, 11, 1),
        )

    def _entry(self, day, direction, amount):
        return BankTransaction.objects.create(
            company=self.company, account=self.account, transaction_date=day,
            direction=direction, category="Тест", amount=Decimal(amount), created_by=self.user,
        )

    def test_period_opening_and_closing_balance_include_only_valid_transactions(self):
        self._entry(date(2026, 11, 1), BankTransaction.INCOME, "100.00")
        self._entry(date(2026, 11, 5), BankTransaction.EXPENSE, "40.00")
        self._entry(date(2026, 11, 20), BankTransaction.INCOME, "25.00")
        voided = self._entry(date(2026, 11, 12), BankTransaction.EXPENSE, "500.00")
        voided.is_void = True
        voided.save(update_fields=["is_void"])
        response = self.client.get("/bank/?from=2026-11-10&to=2026-11-30")
        self.assertEqual(response.status_code, 200)
        row = response.context["balances"][0]
        self.assertEqual(row["opening_at_period"], Decimal("1060.00"))
        self.assertEqual(row["period_income"], Decimal("25.00"))
        self.assertEqual(row["period_expense"], Decimal("0.00"))
        self.assertEqual(row["closing_at_period"], Decimal("1085.00"))

    def test_period_before_opening_date_has_no_claimed_balance(self):
        response = self.client.get("/bank/?from=2026-10-01&to=2026-10-31")
        self.assertEqual(response.status_code, 200)
        row = response.context["balances"][0]
        self.assertIsNone(row["opening_at_period"])
        self.assertIsNone(row["closing_at_period"])

    def test_new_bank_account_form_exposes_opening_balance_fields(self):
        response = self.client.get("/bank/")
        self.assertContains(response, "opening_balance")
        self.assertContains(response, "opening_balance_date")
