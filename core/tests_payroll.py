import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core import payroll, services
from core.models import Customer, PayPlan, Product, Sale, SaleItem, Supplier, SupplierItem

D = Decimal


class PayrollTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        self.anna = U.objects.create_user("anna", password="x", first_name="Анна")
        self.petro = U.objects.create_user("petro", password="x")
        self.adm, self.ca, self.cp = self.client, self.client_class(), self.client_class()
        self.adm.login(username="admin", password="x")
        self.ca.login(username="anna", password="x")
        self.cp.login(username="petro", password="x")
        for c in (self.ca, self.cp):
            c.post("/shift/", {"action": "open", "cash": "0"})
        self.p = Product.objects.create(name="Свічка", sku="SV1", brand="NGK", price=100, cost_price=60, current_stock=50)
        sup = Supplier.objects.create(name="S")
        self.si = SupplierItem.objects.create(supplier=sup, brand="BIL", sku="A1", sku_norm="A1", cost=1000, name="Амортизатор")
        self.month = (timezone.localdate().replace(day=1), timezone.localdate())

    def sell(self, client, items, customer=None):
        r = client.post("/pos/checkout/", json.dumps({"items": items, "payment": "cash", "customer_id": customer.pk if customer else None}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return Sale.objects.get(pk=r.json()["id"])

    def row(self, user, **kw):
        rows, _ = payroll.compute(*self.month, **kw)
        return {r["user"].pk: r for r in rows}.get(user.pk)

    def test_percent_of_margin_with_default_and_personal_plan_and_negative_margin(self):
        PayPlan.objects.create(user=None, margin_pct=10, fixed_monthly=0)                     # типовий
        PayPlan.objects.create(user=self.petro, margin_pct=20, fixed_monthly=0)
        self.sell(self.ca, [{"id": self.p.pk, "qty": 5}])                                    # маржа 5×40 = 200
        self.sell(self.cp, [{"id": self.p.pk, "qty": 5}])
        a, p = self.row(self.anna), self.row(self.petro)
        self.assertEqual((a["margin"], a["pct"], a["variable"], a["own_plan"]), (D("200"), D("10"), D("20.00"), False))
        self.assertEqual((p["margin"], p["pct"], p["variable"], p["own_plan"]), (D("200"), D("20"), D("40.00"), True))
        # продаж нижче собівартості: маржа відʼємна, змінна частина не менше 0
        cheap = Product.objects.create(name="Збиток", sku="L1", brand="X", price=10, cost_price=100, current_stock=5)
        self.sell(self.ca, [{"id": cheap.pk, "qty": 3}])                                      # −270
        a = self.row(self.anna)
        self.assertEqual((a["margin"], a["variable"]), (D("-70"), D("0.00")))

    def test_returns_reduce_margin_and_pay(self):
        PayPlan.objects.create(user=None, margin_pct=10)
        s = self.sell(self.ca, [{"id": self.p.pk, "qty": 5}])
        it = s.items.get()
        self.adm.post(f"/sales/{s.pk}/return/", {f"ret_{it.pk}": "2"})
        self.assertEqual(self.row(self.anna)["margin"], D("120"))                             # (5−2)×40
        self.assertEqual(self.row(self.anna)["variable"], D("12.00"))

    def test_supplier_item_margin_earned_only_when_customer_takes_it(self):
        PayPlan.objects.create(user=None, margin_pct=10)
        cust = Customer.objects.create(name="Іван", phone="1")
        s = self.sell(self.ca, [{"si": self.si.pk, "qty": 2, "price": 1300}], cust)           # маржа 2×300 = 600
        it = s.items.get()
        self.assertIsNone(it.earned_at)
        r = self.row(self.anna)
        self.assertEqual((r["margin"], r["pending"], r["variable"]), (D("0"), D("600"), D("0.00")))     # поки «в дорозі»
        self.assertEqual(self.row(self.anna, include_pending=True)["variable"], D("60.00"))              # за бажанням з урахуванням
        self.ca.post(f"/orders/{it.pk}/arrived/", {"days": "14"})
        self.assertEqual(self.row(self.anna)["margin"], D("0"))                                           # прибув, але не забрали
        self.ca.post(f"/orders/{it.pk}/issued/")
        r = self.row(self.anna)
        self.assertEqual((r["margin"], r["pending"], r["variable"]), (D("600"), D("0"), D("60.00")))
        # якщо клієнт не забрав і товар повернули: маржі немає
        s2 = self.sell(self.ca, [{"si": self.si.pk, "qty": 1, "price": 1300}], cust)
        it2 = s2.items.get()
        self.ca.post(f"/orders/{it2.pk}/arrived/", {"days": "14"})
        self.ca.post(f"/orders/{it2.pk}/supplier_return/")
        self.assertEqual(self.row(self.anna)["margin"], D("600"))
        self.assertEqual(self.row(self.anna)["pending"], D("0"))

    def test_period_uses_earned_date_and_fixed_part_is_prorated(self):
        PayPlan.objects.create(user=None, margin_pct=10, fixed_monthly=D("3000"))
        s = self.sell(self.ca, [{"id": self.p.pk, "qty": 5}])
        SaleItem.objects.filter(sale=s).update(earned_at=timezone.now() - timedelta(days=60))             # зароблено давно
        rows, _ = payroll.compute(*self.month)
        self.assertEqual({r["user"].pk: r["margin"] for r in rows}.get(self.anna.pk, D("0")), D("0"))
        full = payroll.compute(date(2026, 2, 1), date(2026, 2, 28))[1]["fixed"]
        self.assertEqual(full, D("6000.00"))                  # повний місяць: фіксована всім менеджерам (anna, petro), навіть без продажів; власнику (admin) ні
        self.assertEqual(payroll.months_fraction(date(2026, 2, 1), date(2026, 2, 28)), D(1))
        self.assertEqual(payroll.months_fraction(date(2026, 1, 16), date(2026, 1, 31)), D(16) / 31)
        self.assertEqual(payroll.months_fraction(date(2026, 1, 15), date(2026, 2, 14)), D(17) / 31 + D(14) / 28)

    def test_pages_permissions_csv_and_manager_sees_only_own_commission(self):
        PayPlan.objects.create(user=None, margin_pct=10)
        PayPlan.objects.create(user=self.petro, margin_pct=25, fixed_monthly=0)
        self.sell(self.ca, [{"id": self.p.pk, "qty": 5}])
        self.sell(self.cp, [{"id": self.p.pk, "qty": 5}])
        page = self.adm.get("/payroll/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Анна")
        self.assertContains(page, "до виплати")
        csv = self.adm.get("/payroll/?csv=1").content.decode("utf-8-sig")
        self.assertIn("anna;1;500", csv)
        self.assertIn("petro;1;500", csv)
        for u in ("/payroll/", "/pay-plans/", "/pay-plans/new/"):
            self.assertEqual(self.ca.get(u).status_code, 403, u)
        mine = self.ca.get("/my-sales/")
        self.assertContains(mine, "Моя комісія за період")
        self.assertEqual(mine.context["mypay"]["variable"], D("20.00"))                       # 10% від 200
        self.assertEqual(mine.context["mypay"]["pct"], D("10"))                               # ставку petro (25%) не видно
        self.assertNotContains(mine, "petro")
        self.assertEqual(self.cp.get("/my-sales/").context["mypay"]["variable"], D("50.00"))
        PayPlan.objects.all().delete()
        self.assertIsNone(self.ca.get("/my-sales/").context["mypay"])                         # без плану картки немає

    def test_plan_form_validation_single_default_and_unique_user(self):
        f = {"user": "", "margin_pct": "10", "fixed_monthly": "0", "note": ""}
        self.assertEqual(self.adm.post("/pay-plans/new/", f).status_code, 302)
        self.assertContains(self.adm.post("/pay-plans/new/", f), "Типовий план уже існує")
        self.assertContains(self.adm.post("/pay-plans/new/", dict(f, user=self.anna.pk, margin_pct="150")), "від 0 до 100")
        self.assertContains(self.adm.post("/pay-plans/new/", dict(f, user=self.anna.pk, fixed_monthly="-5")), "від&#x27;ємною")
        self.assertEqual(self.adm.post("/pay-plans/new/", dict(f, user=self.anna.pk, margin_pct="12.5")).status_code, 302)
        self.assertEqual(self.adm.post("/pay-plans/new/", dict(f, user=self.anna.pk)).status_code, 200)       # другий план тому ж менеджеру: відмова
        self.assertEqual(PayPlan.objects.count(), 2)
        self.assertContains(self.adm.get("/pay-plans/"), "Типовий план (для всіх)")
