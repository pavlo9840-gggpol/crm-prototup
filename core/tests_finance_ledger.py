from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import FinanceEntry, Shift
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


class SettlementLedgerTests(TestCase):
    def setUp(self):
        from core.models import Customer, Supplier, Sale, Purchase, SettlementPayment, Shift, CashOperation
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="settlement-owner", email="", password="secret")
        self.cashier = User.objects.create_user(username="settlement-cashier", password="secret")
        self.company = create_test_company(self.owner, "Settlement shop", [(self.cashier, "cashier")])
        self.customer = Customer.objects.create(company=self.company, name="Клієнт", phone="0500000011")
        self.supplier = Supplier.objects.create(company=self.company, name="Постачальник")
        self.sale = Sale.objects.create(company=self.company, cashier=self.owner, customer=self.customer,
                                        total=Decimal("1000.00"), cash_amount=Decimal("300.00"),
                                        card_amount=Decimal("0.00"))
        self.purchase = Purchase.objects.create(company=self.company, supplier=self.supplier, user=self.owner,
                                                total=Decimal("2000.00"))
        self.Payment = SettlementPayment

    def test_supplier_purchase_accepts_multiple_partial_payments_and_updates_balance(self):
        self.client.force_login(self.owner)
        url = reverse("settlement_payment_new", args=["purchase", self.purchase.pk])
        for amount in ("500.00", "700.00"):
            response = self.client.post(url, {"amount": amount, "payment_method": "bank", "document_ref": "PAY-1"})
            self.assertEqual(response.status_code, 302)
        self.purchase.refresh_from_db()
        response = self.client.get(reverse("purchase_detail", args=[self.purchase.pk]))
        self.assertEqual(response.context["settlement_paid"], Decimal("1200.00"))
        self.assertEqual(response.context["settlement_balance"], Decimal("800.00"))
        self.assertEqual(self.Payment.objects.filter(purchase=self.purchase, is_void=False).count(), 2)

    def test_customer_sale_receipt_cannot_exceed_outstanding_balance(self):
        Shift.objects.create(company=self.company, cashier=self.owner, drawer={})
        self.client.force_login(self.owner)
        url = reverse("settlement_payment_new", args=["sale", self.sale.pk])
        response = self.client.post(url, {"amount": "700.00", "payment_method": "cash"})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(url, {"amount": "1.00", "payment_method": "cash"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.Payment.objects.filter(sale=self.sale, is_void=False).count(), 1)
        detail = self.client.get(reverse("sale_detail", args=[self.sale.pk]))
        self.assertEqual(detail.context["settlement_balance"], Decimal("0.00"))

    def test_payment_void_restores_debt_and_keeps_audit_trail(self):
        shift = Shift.objects.create(company=self.company, cashier=self.owner, drawer={"20000": 2})
        self.client.force_login(self.owner)
        response = self.client.post(reverse("settlement_payment_new", args=["purchase", self.purchase.pk]), {
            "amount": "400.00", "payment_method": "cash",
        })
        self.assertEqual(response.status_code, 302)
        payment = self.Payment.objects.get(purchase=self.purchase, is_void=False)
        self.assertEqual(payment.cash_operation.kind, "out")
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {})
        response = self.client.post(reverse("settlement_payment_void", args=[payment.pk]), {"reason": "Помилкова оплата"})
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        shift.refresh_from_db()
        self.assertTrue(payment.is_void)
        self.assertEqual(payment.void_reason, "Помилкова оплата")
        self.assertEqual(payment.voided_by, self.owner)
        self.assertIsNotNone(payment.reversal_operation_id)
        self.assertEqual(payment.reversal_operation.kind, "in")
        self.assertEqual(shift.drawer, {"20000": 2})
        detail = self.client.get(reverse("purchase_detail", args=[self.purchase.pk]))
        self.assertEqual(detail.context["settlement_paid"], Decimal("0.00"))
        self.assertEqual(detail.context["settlement_balance"], Decimal("2000.00"))

    def test_foreign_document_and_cashier_cannot_register_payment(self):
        other_owner = get_user_model().objects.create_superuser(username="other-settlement-owner", email="", password="secret")
        other_company = create_test_company(other_owner, "Other settlement shop", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Інший")
        other_purchase = Purchase.objects.create(company=other_company, supplier=other_supplier,
            user=other_owner, total=Decimal("100.00"))
        self.client.force_login(self.cashier)
        response = self.client.post(reverse("settlement_payment_new", args=["purchase", other_purchase.pk]),
                                    {"amount": "10.00", "payment_method": "cash"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.Payment.objects.filter(purchase=other_purchase).count(), 0)

    def test_customer_cash_settlement_updates_drawer_and_void_posts_compensating_movement(self):
        shift = Shift.objects.create(company=self.company, cashier=self.owner, drawer={})
        self.client.force_login(self.owner)
        response = self.client.post(reverse("settlement_payment_new", args=["sale", self.sale.pk]), {
            "amount": "250.00", "payment_method": "cash",
        })
        self.assertEqual(response.status_code, 302)
        payment = self.Payment.objects.get(sale=self.sale, is_void=False)
        self.assertEqual(payment.cash_operation.kind, "in")
        shift.refresh_from_db()
        self.assertEqual(sum(int(k) * v for k, v in shift.drawer.items()), 25000)
        self.client.post(reverse("settlement_payment_void", args=[payment.pk]), {"reason": "Тест сторнування"})
        payment.refresh_from_db()
        shift.refresh_from_db()
        self.assertTrue(payment.is_void)
        self.assertEqual(payment.reversal_operation.kind, "out")
        self.assertEqual(shift.drawer, {})

    def test_supplier_cash_payment_rejects_insufficient_drawer_and_records_outflow(self):
        shift = Shift.objects.create(company=self.company, cashier=self.owner, drawer={"20000": 1})
        self.client.force_login(self.owner)
        url = reverse("settlement_payment_new", args=["purchase", self.purchase.pk])
        response = self.client.post(url, {"amount": "400.00", "payment_method": "cash"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.Payment.objects.filter(purchase=self.purchase).exists())
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {"20000": 1})
        shift.drawer = {"20000": 2}
        shift.save(update_fields=["drawer"])
        response = self.client.post(url, {"amount": "400.00", "payment_method": "cash"})
        self.assertEqual(response.status_code, 302)
        payment = self.Payment.objects.get(purchase=self.purchase)
        self.assertEqual(payment.cash_operation.kind, "out")
        shift.refresh_from_db()
        self.assertEqual(shift.drawer, {})

    def test_cash_settlement_requires_an_open_shift(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("settlement_payment_new", args=["sale", self.sale.pk]), {
            "amount": "100.00", "payment_method": "cash",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.Payment.objects.filter(sale=self.sale).exists())

    def test_cash_settlement_cannot_be_voided_after_its_shift_is_closed(self):
        shift = Shift.objects.create(company=self.company, cashier=self.owner, drawer={})
        self.client.force_login(self.owner)
        self.client.post(reverse("settlement_payment_new", args=["sale", self.sale.pk]), {
            "amount": "100.00", "payment_method": "cash",
        })
        payment = self.Payment.objects.get(sale=self.sale)
        shift.refresh_from_db()
        shift.closed_at = timezone.now()
        shift.save(update_fields=["closed_at"])
        self.client.post(reverse("settlement_payment_void", args=[payment.pk]), {"reason": "Помилка"})
        payment.refresh_from_db()
        self.assertFalse(payment.is_void)
        self.assertIsNone(payment.reversal_operation_id)

    def test_customer_account_shows_customer_debt_and_payment_history(self):
        self.Payment.objects.create(company=self.company, sale=self.sale,
            amount=Decimal("200.00"), payment_method="bank", created_by=self.owner)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("customer_account", args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["account_totals"]["sales"], Decimal("1000.00"))
        self.assertEqual(response.context["account_totals"]["checkout_paid"], Decimal("300.00"))
        self.assertEqual(response.context["account_totals"]["later_paid"], Decimal("200.00"))
        self.assertEqual(response.context["account_totals"]["debt"], Decimal("500.00"))
        self.assertContains(response, "Поточний борг")
        self.assertContains(response, "Історія додаткових оплат")

    def test_supplier_account_shows_purchase_payments_and_outstanding_balance(self):
        self.Payment.objects.create(company=self.company, purchase=self.purchase,
            amount=Decimal("650.00"), payment_method="bank", created_by=self.owner)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("supplier_account", args=[self.supplier.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["account_totals"]["purchases"], Decimal("2000.00"))
        self.assertEqual(response.context["account_totals"]["paid"], Decimal("650.00"))
        self.assertEqual(response.context["account_totals"]["debt"], Decimal("1350.00"))
        self.assertContains(response, "Постачальник")

    def test_customer_and_supplier_accounts_are_company_isolated(self):
        other_owner = get_user_model().objects.create_superuser(username="foreign-account-owner", email="", password="secret")
        other_company = create_test_company(other_owner, "Foreign account", [])
        from core.models import Customer, Supplier
        foreign_customer = Customer.objects.create(company=other_company, name="Чужий клієнт", phone="0509999991")
        foreign_supplier = Supplier.objects.create(company=other_company, name="Чужий постачальник")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("customer_account", args=[foreign_customer.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("supplier_account", args=[foreign_supplier.pk])).status_code, 404)

    def test_model_rejects_payment_linked_to_foreign_company_document(self):
        other_owner = get_user_model().objects.create_superuser(
            username="foreign-settlement-owner", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Foreign settlement ledger", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Чужий постачальник")
        other_purchase = Purchase.objects.create(company=other_company, supplier=other_supplier,
            user=other_owner, total=Decimal("100.00"))
        with self.assertRaises(ValueError):
            self.Payment.objects.create(company=self.company, purchase=other_purchase,
                amount=Decimal("10.00"), payment_method="cash", created_by=self.owner)
