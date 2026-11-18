from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import FinanceEntry
from core.tests_tenant_helpers import create_test_company


class FinanceLedgerTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="finance-owner", email="", password="secret")
        self.cashier = User.objects.create_user(username="finance-cashier", password="secret")
        self.company = create_test_company(self.owner, "Finance ledger shop", [(self.cashier, "cashier")])

    def test_owner_can_record_income_and_expense_and_view_net_total(self):
        self.client.force_login(self.owner)
        url = reverse("finance_ledger")
        for payload in (
            {"entry_type": "income", "category": "Інші доходи", "amount": "1500.00", "payment_method": "bank",
             "description": "Повернення переплати", "document_ref": "BANK-01"},
            {"entry_type": "expense", "category": "Оренда", "amount": "500.00", "payment_method": "bank",
             "description": "Оренда приміщення", "document_ref": "INV-02"},
        ):
            response = self.client.post(url, payload)
            self.assertEqual(response.status_code, 302)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["income"], Decimal("1500.00"))
        self.assertEqual(response.context["expense"], Decimal("500.00"))
        self.assertEqual(response.context["balance"], Decimal("1000.00"))
        self.assertEqual(FinanceEntry.objects.filter(company=self.company).count(), 2)
        self.assertEqual(FinanceEntry.objects.filter(company=self.company, created_by=self.owner).count(), 2)

    def test_invalid_or_non_positive_amount_is_not_saved(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("finance_ledger"), {
            "entry_type": "expense", "category": "Оренда", "amount": "0",
            "payment_method": "cash", "description": "", "document_ref": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FinanceEntry.objects.filter(company=self.company).count(), 0)

    def test_cashier_cannot_manage_finance_ledger(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse("finance_ledger"))
        self.assertEqual(response.status_code, 403)

    def test_finance_entries_are_isolated_by_company(self):
        other_owner = get_user_model().objects.create_superuser(username="other-finance-owner", email="", password="secret")
        other_company = create_test_company(other_owner, "Other finance shop", [])
        FinanceEntry.objects.create(company=other_company, entry_type="income", category="Other",
                                    amount=Decimal("999.00"), payment_method="bank", created_by=other_owner)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("finance_ledger"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "999.00")
        self.assertEqual(response.context["income"], Decimal("0.00"))


    def test_voided_entry_remains_auditable_but_is_excluded_from_totals(self):
        entry = FinanceEntry.objects.create(
            company=self.company, entry_type="expense", category="Оренда",
            amount=Decimal("700.00"), payment_method="cash", created_by=self.owner,
        )
        self.client.force_login(self.owner)
        response = self.client.post(reverse("finance_entry_void", args=[entry.pk]), {
            "reason": "Помилково внесено",
        })
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertTrue(entry.is_void)
        self.assertEqual(entry.void_reason, "Помилково внесено")
        self.assertEqual(entry.voided_by, self.owner)
        self.assertIsNotNone(entry.voided_at)

        ledger = self.client.get(reverse("finance_ledger"))
        self.assertEqual(ledger.context["expense"], Decimal("0.00"))
        self.assertContains(ledger, "Помилково внесено")
        self.assertEqual(FinanceEntry.objects.filter(company=self.company).count(), 1)

    def test_cannot_void_entry_from_another_company_or_void_twice(self):
        other_owner = get_user_model().objects.create_superuser(
            username="foreign-finance-owner", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Foreign finance", [])
        foreign = FinanceEntry.objects.create(
            company=other_company, entry_type="income", category="Other",
            amount=Decimal("50.00"), payment_method="bank", created_by=other_owner,
        )
        self.client.force_login(self.owner)
        response = self.client.post(reverse("finance_entry_void", args=[foreign.pk]), {
            "reason": "Not mine",
        })
        self.assertEqual(response.status_code, 302)
        foreign.refresh_from_db()
        self.assertFalse(foreign.is_void)

        own = FinanceEntry.objects.create(
            company=self.company, entry_type="income", category="Other",
            amount=Decimal("50.00"), payment_method="bank", created_by=self.owner,
        )
        url = reverse("finance_entry_void", args=[own.pk])
        self.client.post(url, {"reason": "Correction"})
        self.client.post(url, {"reason": "Second attempt"})
        own.refresh_from_db()
        self.assertEqual(own.void_reason, "Correction")


    def test_payment_method_totals_exclude_voided_entries(self):
        FinanceEntry.objects.create(
            company=self.company, entry_type="income", category="Продаж",
            amount=Decimal("300.00"), payment_method="cash", created_by=self.owner,
        )
        FinanceEntry.objects.create(
            company=self.company, entry_type="expense", category="Дрібні витрати",
            amount=Decimal("80.00"), payment_method="cash", created_by=self.owner,
        )
        FinanceEntry.objects.create(
            company=self.company, entry_type="income", category="Повернення",
            amount=Decimal("900.00"), payment_method="cash", created_by=self.owner, is_void=True,
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("finance_ledger"))
        self.assertEqual(response.context["payment_totals"]["cash"]["income"], Decimal("300.00"))
        self.assertEqual(response.context["payment_totals"]["cash"]["expense"], Decimal("80.00"))
        self.assertEqual(response.context["payment_totals"]["cash"]["balance"], Decimal("220.00"))
