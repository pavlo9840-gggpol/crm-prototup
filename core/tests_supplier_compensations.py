from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    BankAccount, BankTransaction, CashOperation, Product, Purchase, PurchaseItem,
    Shift, Supplier, SupplierCompensation, SupplierReturn,
)
from core.tests_tenant_helpers import create_test_company


class SupplierCompensationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="supplier-comp-owner", email="", password="secret")
        self.company = create_test_company(self.owner, "Supplier compensation shop", [])
        self.client.force_login(self.owner)
        self.supplier = Supplier.objects.create(company=self.company, name="Supplier Compensation Ltd")
        self.product = Product.objects.create(
            company=self.company, name="Brake pad", sku="BP-01",
            current_stock=Decimal("0"), cost_price=Decimal("100.00"),
        )
        self.purchase = Purchase.objects.create(
            company=self.company, supplier=self.supplier, user=self.owner,
            total=Decimal("200.00"),
        )
        self.item = PurchaseItem.objects.create(
            company=self.company, purchase=self.purchase, product=self.product,
            quantity=Decimal("2.000"), cost_price=Decimal("100.00"),
        )
        self.returned = SupplierReturn.objects.create(
            company=self.company, purchase_item=self.item, supplier=self.supplier,
            quantity=Decimal("2.000"), unit_cost=Decimal("100.00"),
            compensation_type="credit", reason="Повернення браку",
            completed_at=timezone.now(), created_by=self.owner,
        )

    def post_compensation(self, payload):
        return self.client.post(
            reverse("supplier_return_compensate", args=[self.returned.pk]), payload
        )

    def test_credit_note_reduces_supplier_debt_and_is_shown_in_purchase(self):
        response = self.post_compensation({"kind": "credit", "amount": "150.00", "note": "Кредит-нота №1"})
        self.assertRedirects(response, reverse("purchase_detail", args=[self.purchase.pk]))
        entry = SupplierCompensation.objects.get(supplier_return=self.returned)
        self.assertEqual(entry.kind, SupplierCompensation.CREDIT)
        self.assertEqual(entry.number, f"SC-{entry.pk:06d}")
        self.assertIsNone(entry.cash_operation_id)
        self.assertIsNone(entry.bank_transaction_id)

        detail = self.client.get(reverse("purchase_detail", args=[self.purchase.pk]))
        self.assertEqual(detail.context["settlement_compensated"], Decimal("150.00"))
        self.assertEqual(detail.context["settlement_balance"], Decimal("50.00"))
        self.assertContains(detail, "Кредит-нота №1")

    def test_partial_compensation_cannot_exceed_return_total(self):
        self.post_compensation({"kind": "credit", "amount": "125.00"})
        self.post_compensation({"kind": "credit", "amount": "75.01"})
        total = sum(
            SupplierCompensation.objects.filter(supplier_return=self.returned).values_list("amount", flat=True),
            Decimal("0.00"),
        )
        self.assertEqual(total, Decimal("125.00"))

    def test_cash_refund_increases_open_cash_drawer_and_records_operation(self):
        shift = Shift.objects.create(
            company=self.company, cashier=self.owner, opening_cash=Decimal("0.00"), drawer={},
        )
        response = self.post_compensation({"kind": "cash", "amount": "50.00"})
        self.assertRedirects(response, reverse("purchase_detail", args=[self.purchase.pk]))
        shift.refresh_from_db()
        operation = CashOperation.objects.get(shift=shift)
        self.assertEqual(operation.kind, "in")
        self.assertEqual(operation.amount, Decimal("50.00"))
        self.assertEqual(operation.detail["source"], "supplier_return_compensation")
        self.assertEqual(shift.drawer, operation.detail["counts"])
        entry = SupplierCompensation.objects.get(supplier_return=self.returned)
        self.assertEqual(entry.cash_operation, operation)
        self.assertEqual(entry.kind, SupplierCompensation.CASH)

    def test_bank_refund_creates_income_transaction_on_company_account(self):
        account = BankAccount.objects.create(
            company=self.company, bank_name="Test bank", iban="UA123456789012345678901234567",
        )
        response = self.post_compensation({
            "kind": "bank", "amount": "80.00", "bank_account": str(account.pk),
            "note": "Повернення за актом",
        })
        self.assertRedirects(response, reverse("purchase_detail", args=[self.purchase.pk]))
        transaction = BankTransaction.objects.get(company=self.company, document_ref=self.returned.number)
        self.assertEqual(transaction.direction, BankTransaction.INCOME)
        self.assertEqual(transaction.amount, Decimal("80.00"))
        self.assertEqual(transaction.account, account)
        entry = SupplierCompensation.objects.get(supplier_return=self.returned)
        self.assertEqual(entry.bank_transaction, transaction)
        self.assertEqual(entry.bank_account, account)

    def test_bank_refund_rejects_account_from_another_company(self):
        other_owner = get_user_model().objects.create_superuser(
            username="supplier-comp-other", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Other supplier compensation shop", [])
        foreign_account = BankAccount.objects.create(
            company=other_company, bank_name="Foreign bank", iban="UA999999999999999999999999999",
        )
        self.post_compensation({
            "kind": "bank", "amount": "20.00", "bank_account": str(foreign_account.pk),
        })
        self.assertFalse(SupplierCompensation.objects.exists())
        self.assertFalse(BankTransaction.objects.filter(company=self.company).exists())

    def test_cash_refund_requires_open_shift_and_does_not_create_partial_ledger_entry(self):
        self.post_compensation({"kind": "cash", "amount": "20.00"})
        self.assertFalse(SupplierCompensation.objects.exists())
        self.assertFalse(CashOperation.objects.exists())

    def test_credit_note_can_be_voided_with_audit_and_debt_is_restored(self):
        self.post_compensation({"kind": "credit", "amount": "60.00"})
        entry = SupplierCompensation.objects.get(supplier_return=self.returned)
        response = self.client.post(
            reverse("supplier_compensation_void", args=[entry.pk]),
            {"reason": "Постачальник відкликав кредит-ноту"},
        )
        self.assertRedirects(response, reverse("purchase_detail", args=[self.purchase.pk]))
        entry.refresh_from_db()
        self.assertTrue(entry.is_void)
        self.assertEqual(entry.void_reason, "Постачальник відкликав кредит-ноту")
        self.assertEqual(entry.voided_by, self.owner)
        self.assertIsNotNone(entry.voided_at)
        detail = self.client.get(reverse("purchase_detail", args=[self.purchase.pk]))
        self.assertEqual(detail.context["settlement_compensated"], Decimal("0.00"))
        self.assertEqual(detail.context["settlement_balance"], Decimal("200.00"))

    def test_cash_compensation_void_creates_outgoing_reversal_and_restores_drawer(self):
        shift = Shift.objects.create(
            company=self.company, cashier=self.owner, opening_cash=Decimal("0.00"), drawer={},
        )
        self.post_compensation({"kind": "cash", "amount": "50.00"})
        entry = SupplierCompensation.objects.get(supplier_return=self.returned)
        self.assertEqual(shift.cash_ops.count(), 1)
        response = self.client.post(
            reverse("supplier_compensation_void", args=[entry.pk]),
            {"reason": "Помилкова сума"},
        )
        self.assertRedirects(response, reverse("purchase_detail", args=[self.purchase.pk]))
        shift.refresh_from_db()
        entry.refresh_from_db()
        self.assertTrue(entry.is_void)
        self.assertIsNotNone(entry.reversal_cash_operation_id)
        self.assertEqual(shift.drawer, {})
        self.assertEqual(shift.cash_ops.count(), 2)
        self.assertEqual(entry.reversal_cash_operation.kind, "out")
        self.assertEqual(entry.reversal_cash_operation.amount, Decimal("-50.00"))

    def test_bank_compensation_void_creates_expense_reversal(self):
        account = BankAccount.objects.create(
            company=self.company, bank_name="Test bank", iban="UA123456789012345678901234567",
        )
        self.post_compensation({"kind": "bank", "amount": "80.00", "bank_account": str(account.pk)})
        entry = SupplierCompensation.objects.get(supplier_return=self.returned)
        response = self.client.post(
            reverse("supplier_compensation_void", args=[entry.pk]),
            {"reason": "Повернення помилково зараховано"},
        )
        self.assertRedirects(response, reverse("purchase_detail", args=[self.purchase.pk]))
        entry.refresh_from_db()
        self.assertTrue(entry.is_void)
        self.assertIsNotNone(entry.reversal_bank_transaction_id)
        self.assertEqual(entry.reversal_bank_transaction.direction, BankTransaction.EXPENSE)
        self.assertEqual(entry.reversal_bank_transaction.amount, Decimal("80.00"))
        self.assertEqual(BankTransaction.objects.filter(company=self.company).count(), 2)

    def test_foreign_company_supplier_return_cannot_be_compensated(self):
        other_owner = get_user_model().objects.create_superuser(
            username="supplier-comp-foreign-owner", email="", password="secret"
        )
        other_company = create_test_company(other_owner, "Foreign return company", [])
        other_supplier = Supplier.objects.create(company=other_company, name="Foreign supplier")
        other_product = Product.objects.create(
            company=other_company, name="Foreign part", sku="FOREIGN-01",
            current_stock=Decimal("0"), cost_price=Decimal("10.00"),
        )
        other_purchase = Purchase.objects.create(
            company=other_company, supplier=other_supplier, user=other_owner,
            total=Decimal("10.00"),
        )
        other_item = PurchaseItem.objects.create(
            company=other_company, purchase=other_purchase, product=other_product,
            quantity=Decimal("1.000"), cost_price=Decimal("10.00"),
        )
        foreign_return = SupplierReturn.objects.create(
            company=other_company, purchase_item=other_item, supplier=other_supplier,
            quantity=Decimal("1.000"), unit_cost=Decimal("10.00"),
            compensation_type="credit", reason="Foreign claim",
            completed_at=timezone.now(), created_by=other_owner,
        )
        response = self.client.post(
            reverse("supplier_return_compensate", args=[foreign_return.pk]),
            {"kind": "credit", "amount": "10.00"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SupplierCompensation.objects.filter(supplier_return=foreign_return).exists())

    def test_supplier_account_shows_credit_after_compensation_exceeds_remaining_debt(self):
        # An earlier payment means a full supplier refund can create a credit balance.
        from core.models import SettlementPayment
        SettlementPayment.objects.create(
            company=self.company, purchase=self.purchase, amount=Decimal("200.00"),
            payment_method="bank", created_by=self.owner,
        )
        self.post_compensation({"kind": "credit", "amount": "200.00"})
        response = self.client.get(reverse("supplier_account", args=[self.supplier.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["account_totals"]["debt"], Decimal("0.00"))
        self.assertEqual(response.context["account_totals"]["supplier_credit"], Decimal("200.00"))

