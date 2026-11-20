from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import (
    AccountAdvance, BankAccount, BankReconciliation, BankTransaction,
    Company, Customer, StaffMembership, Supplier,
)


class BankAdvanceReconciliationTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("bankadvance", "", "test-password")
        self.company = Company.objects.create(name="Bank advance test", slug="bank-advance-test", owner=self.user)
        StaffMembership.objects.create(company=self.company, user=self.user, role="owner")
        self.client.login(username="bankadvance", password="test-password")
        self.customer = Customer.objects.create(company=self.company, name="Customer", phone="+380501112233")
        self.supplier = Supplier.objects.create(company=self.company, name="Supplier")
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test bank", iban="UA123456789012345678901234567",
        )

    def _entry(self, direction, amount="125.00"):
        return BankTransaction.objects.create(
            company=self.company, account=self.account, transaction_date=timezone.localdate(),
            direction=direction, category="Перевірка авансу", amount=Decimal(amount),
            created_by=self.user,
        )

    def _advance(self, party_type, payment_method="bank", amount="125.00"):
        party = self.customer if party_type == AccountAdvance.CUSTOMER else self.supplier
        kwargs = {"customer": party} if party_type == AccountAdvance.CUSTOMER else {"supplier": party}
        return AccountAdvance.objects.create(
            company=self.company, party_type=party_type, amount=Decimal(amount),
            remaining=Decimal(amount), payment_method=payment_method,
            created_by=self.user, **kwargs,
        )

    def test_customer_advance_can_be_reconciled_and_cancelled(self):
        advance = self._advance(AccountAdvance.CUSTOMER)
        entry = self._entry(BankTransaction.INCOME)
        response = self.client.post(f"/bank/{entry.pk}/reconcile/", {"match_ref": f"advance:{advance.pk}"})
        self.assertEqual(response.status_code, 302)
        match = BankReconciliation.objects.get(bank_transaction=entry, is_void=False)
        self.assertEqual(match.account_advance_id, advance.pk)
        self.assertIsNone(match.settlement_payment_id)
        response = self.client.post(f"/bank/reconciliation/{match.pk}/void/", {"reason": "Помилкова звірка"})
        self.assertEqual(response.status_code, 302)
        match.refresh_from_db()
        self.assertTrue(match.is_void)

    def test_supplier_advance_matches_only_expense_direction(self):
        advance = self._advance(AccountAdvance.SUPPLIER)
        wrong = self._entry(BankTransaction.INCOME)
        self.client.post(f"/bank/{wrong.pk}/reconcile/", {"match_ref": f"advance:{advance.pk}"})
        self.assertFalse(BankReconciliation.objects.filter(bank_transaction=wrong, is_void=False).exists())
        right = self._entry(BankTransaction.EXPENSE)
        self.client.post(f"/bank/{right.pk}/reconcile/", {"match_ref": f"advance:{advance.pk}"})
        self.assertTrue(BankReconciliation.objects.filter(bank_transaction=right, account_advance=advance, is_void=False).exists())

    def test_advance_cannot_be_matched_twice_or_across_tenants(self):
        advance = self._advance(AccountAdvance.CUSTOMER)
        first = self._entry(BankTransaction.INCOME)
        second = self._entry(BankTransaction.INCOME)
        self.client.post(f"/bank/{first.pk}/reconcile/", {"match_ref": f"advance:{advance.pk}"})
        self.client.post(f"/bank/{second.pk}/reconcile/", {"match_ref": f"advance:{advance.pk}"})
        self.assertEqual(BankReconciliation.objects.filter(account_advance=advance, is_void=False).count(), 1)
        foreign_company = Company.objects.create(name="Other", slug="other-bank-advance", owner=self.user)
        foreign_customer = Customer.objects.create(company=foreign_company, name="Other", phone="+380501112234")
        foreign = AccountAdvance.objects.create(
            company=foreign_company, party_type=AccountAdvance.CUSTOMER, customer=foreign_customer,
            amount=Decimal("125.00"), remaining=Decimal("125.00"), payment_method="bank",
            created_by=self.user,
        )
        third = self._entry(BankTransaction.INCOME)
        self.client.post(f"/bank/{third.pk}/reconcile/", {"match_ref": f"advance:{foreign.pk}"})
        self.assertFalse(BankReconciliation.objects.filter(bank_transaction=third, is_void=False).exists())

    def test_model_rejects_cash_advance_and_wrong_amount(self):
        cash_advance = self._advance(AccountAdvance.CUSTOMER, payment_method="cash")
        entry = self._entry(BankTransaction.INCOME)
        with self.assertRaises(ValueError):
            BankReconciliation.objects.create(
                company=self.company, bank_transaction=entry, account_advance=cash_advance, matched_by=self.user,
            )
        bank_advance = self._advance(AccountAdvance.CUSTOMER, amount="80.00")
        with self.assertRaises(ValueError):
            BankReconciliation.objects.create(
                company=self.company, bank_transaction=entry, account_advance=bank_advance, matched_by=self.user,
            )
