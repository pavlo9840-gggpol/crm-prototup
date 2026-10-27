import json
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Customer, MarkupTier, Product, Purchase, Receipt, Sale, SaleItem, StockMovement, Supplier, SupplierItem
from core.tests_transit import xlsx_bytes

D = Decimal
HEAD = ["Бренд", "Артикул", "Назва", "Кількість", "Ціна"]


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class ReceivingTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("anna", password="x")
        U.objects.create_user("petro", password="x")
        self.adm, self.anna, self.petro = self.client, self.client_class(), self.client_class()
        self.adm.login(username="admin", password="x")
        self.anna.login(username="anna", password="x")
        self.petro.login(username="petro", password="x")
        for c in (self.anna, self.petro):
            c.post("/shift/", {"action": "open", "cash": "0"})
        self.sup = Supplier.objects.create(name="Busmarket")
        self.bil = self.si("BILSTEIN", "19-152615", 1000, "Амортизатор задній")
        self.sol = self.si("SOLGY", "112021", 500, "Кран печі")
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        self.c1 = Customer.objects.create(name="Іван", phone="1")
        self.c2 = Customer.objects.create(name="Олена", phone="2")

    def si(self, brand, sku, cost, name=""):
        return SupplierItem.objects.create(supplier=self.sup, brand=brand, sku=sku, sku_norm="".join(c for c in sku if c.isalnum()).upper(),
                                           cost=cost, name=name)

    def order(self, client, si, qty, customer=None, price=1300):
        r = client.post("/pos/checkout/", json.dumps({"items": [{"si": si.pk, "qty": qty, "price": price}], "payment": "cash",
                                                      "customer_id": customer.pk if customer else None}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return Sale.objects.get(pk=r.json()["id"]).items.get()

    def invoice(self, rows, head=HEAD, number="ДГ-1", client=None, days="", apply=True):
        client = client or self.adm
        f = SimpleUploadedFile("inv.xlsx", xlsx_bytes([head] + rows))
        r = client.post("/receive/preview/", {"supplier": self.sup.pk, "file": f, "number": number, "days": days or "14"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        if not apply:
            return r
        ctx = r.context
        data = {"supplier": self.sup.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": ctx["hdr"], "number": number, "days": days or "14"}
        data.update({f"col_{f['key']}": f["sel"] for f in ctx["fields"] if f["sel"] is not None})
        return client.post("/receive/apply/", data, follow=True), data

    def refresh(self, *items):
        return [SaleItem.objects.get(pk=i.pk) for i in items]

    # ---------------- головний сценарій
    def test_full_invoice_matches_orders_fifo_and_stocks_the_rest(self):
        a = self.order(self.anna, self.bil, 2, self.c1)
        b = self.order(self.petro, self.bil, 1, self.c2)
        c = self.order(self.anna, self.sol, 1)
        r = self.invoice([["Bilstein", "19 152 615", "Амортизатор", 3, 1000], ["SOLGY", "112021", "Кран", 2, 520],
                          ["MEYLE", "N-1", "Нова деталь", 4, 50]], apply=False)
        self.assertEqual(r.context["plan"]["totals"], {"matched": 3, "stock_lines": 2, "errors": 0, "pending_after": 0})
        self.assertEqual({i.fulfil_status for i in self.refresh(a, b, c)}, {"ordered"})          # перегляд нічого не змінює
        self.assertEqual(Receipt.objects.count(), 0)
        resp, _ = self.invoice([["Bilstein", "19 152 615", "Амортизатор", 3, 1000], ["SOLGY", "112021", "Кран", 2, 520],
                                ["MEYLE", "N-1", "Нова деталь", 4, 50]])
        self.assertContains(resp, "під клієнтів 3 поз., на склад 2 рядків")
        a, b, c = self.refresh(a, b, c)
        for it in (a, b, c):
            self.assertEqual((it.fulfil_status, it.receipt_ref, it.pickup_deadline), ("arrived", "ДГ-1", timezone.localdate() + timedelta(days=14)))
        self.assertEqual(c.cost_price, D("520.00"))                                              # фактична ціна з накладної
        sol = Product.objects.get(sku="112021")
        self.assertEqual(sol.current_stock, D("1"))                                              # 2 прийшло, 1 під клієнта
        new = Product.objects.get(brand="MEYLE", sku="N-1")
        self.assertEqual((new.current_stock, new.cost_price, new.name), (D("4"), D("50"), "Нова деталь"))
        self.assertEqual(new.price, D("65.00"))                                                   # продається з типовою націнкою 30%
        warn = [w for r in self.adm.post("/receive/preview/", {"supplier": self.sup.pk, "paste": "MEYLE;ZZ-1;1;10"}).context["plan"]["rows"] for w in r["warnings"]]
        self.assertTrue(any("нова деталь" in w for w in warn))
        self.assertEqual(StockMovement.objects.filter(product=a.product).count(), 0)             # замовлені клієнтам минули склад
        pur = Purchase.objects.get()
        self.assertEqual(pur.items.count(), 2)
        rec = Receipt.objects.get()
        self.assertEqual((rec.n_matched, rec.n_stock, rec.number, rec.purchase_id), (3, 2, "ДГ-1", pur.pk))
        self.assertIn("→ склад", rec.note)
        # менеджер petro бачить у своїх замовленнях, що товар прибув, з відліком
        self.assertContains(self.petro.get("/orders/?st=arrived"), "залишилось 14 дн.")

    def test_fifo_earlier_order_first_and_partial_split_keeps_sale_total(self):
        a = self.order(self.anna, self.bil, 3, self.c1)
        b = self.order(self.petro, self.bil, 1, self.c2)
        sale_total = a.sale.total
        self.invoice([["BILSTEIN", "19-152615", "", 1, 1000]])
        a = SaleItem.objects.get(pk=a.pk)
        arrived = SaleItem.objects.get(sale=a.sale, fulfil_status="arrived")
        self.assertEqual((arrived.quantity, a.quantity, a.fulfil_status), (1, 2, "ordered"))     # частина прибула, решта чекає
        self.assertEqual(arrived.quantity + a.quantity, 3)
        self.assertEqual(sum(i.quantity * i.price for i in a.sale.items.all()), sale_total)      # сума чека та сама
        self.assertEqual(SaleItem.objects.get(pk=b.pk).fulfil_status, "ordered")                 # черга: спершу ранніше замовлення
        self.assertEqual(Product.objects.get(sku="19-152615").current_stock, 0)
        self.invoice([["BILSTEIN", "19-152615", "", 3, 1000]], number="ДГ-2")
        self.assertEqual({i.fulfil_status for i in SaleItem.objects.filter(source="supplier")}, {"arrived"})
        self.assertEqual(Product.objects.get(sku="19-152615").current_stock, 0)

    # ---------------- бренд, дубль, права, помилки
    def test_missing_brand_resolved_when_unique_and_flagged_when_ambiguous(self):
        a = self.order(self.anna, self.bil, 1)
        self.si("SACHS", "19-152615", 900, "Той самий номер, інший виробник")
        sachs = SupplierItem.objects.get(brand="SACHS")
        r = self.invoice([["", "19-152615", "", 1, 1000]], apply=False)
        row = r.context["plan"]["rows"][0]
        self.assertEqual((row["brand"], len(row["alloc"])), ("BILSTEIN", 1))                     # однозначно за відкритим замовленням
        self.order(self.petro, sachs, 1)
        r = self.invoice([["", "19-152615", "", 2, 1000]], apply=False)
        self.assertIn("кількох брендів", r.context["plan"]["rows"][0]["error"])
        self.assertEqual(r.context["plan"]["totals"]["errors"], 1)
        resp, _ = self.invoice([["", "19-152615", "", 2, 1000], ["SOLGY", "112021", "", 1, 500]])
        self.assertContains(resp, "пропущено 1")                                                 # неоднозначний рядок пропущено, решта прийнята
        self.assertEqual(SaleItem.objects.get(pk=a.pk).fulfil_status, "ordered")

    def test_second_apply_is_rejected_and_nothing_matches_means_error(self):
        self.order(self.anna, self.bil, 1)
        resp, data = self.invoice([["BILSTEIN", "19-152615", "", 1, 1000], ["MEYLE", "X-9", "Дет", 2, 10]])
        self.assertEqual(Receipt.objects.count(), 1)
        stock_before = Product.objects.get(sku="X-9").current_stock
        r = self.adm.post("/receive/apply/", data, follow=True)                                  # повторне натискання
        self.assertContains(r, "втрачено")
        self.assertEqual((Receipt.objects.count(), Product.objects.get(sku="X-9").current_stock), (1, stock_before))
        resp = self.invoice([["", "UNKNOWN-1", "", 1, 5]], apply=False)
        self.assertFalse(resp.context["can_apply"])
        self.assertNotContains(resp, "Прийняти накладну")

    def test_any_manager_can_receive_other_managers_orders_but_no_foreign_supplier(self):
        petro_item = self.order(self.petro, self.bil, 1, self.c2)
        resp, _ = self.invoice([["BILSTEIN", "19-152615", "", 1, 1000]], client=self.anna)       # приймає anna, замовлення petro
        self.assertEqual(SaleItem.objects.get(pk=petro_item.pk).fulfil_status, "arrived")
        self.assertContains(self.anna.get("/receive/"), "ДГ-1")
        self.assertNotContains(self.petro.get("/receive/"), "ДГ-1")                              # журнал: лише свої (не адмін)
        self.assertContains(self.adm.get("/receive/"), "ДГ-1")
        other = Supplier.objects.create(name="Other")
        f = SimpleUploadedFile("i.xlsx", xlsx_bytes([HEAD, ["BILSTEIN", "19-152615", "", 1, 1]]))
        r = self.adm.post("/receive/preview/", {"supplier": other.pk, "file": f})
        self.assertEqual(r.context["plan"]["totals"]["matched"], 0)                              # чужому постачальнику не зараховується
        self.assertEqual(self.anna.get("/receive/").status_code, 200)

    def test_pasted_text_without_header_and_substitution_warning(self):
        a = self.order(self.anna, self.bil, 1)
        r = self.adm.post("/receive/preview/", {"supplier": self.sup.pk, "paste": "BILSTEIN;19-152615;1;1000\nSOLGY;112021;2;500"})
        self.assertEqual((r.context["hdr"], r.context["plan"]["totals"]["matched"], r.context["plan"]["totals"]["stock_lines"]), (0, 1, 1))
        # прийшов аналог замість замовленого: попередження, але нічого не підміняється самостійно
        from core.models import PartOe
        PartOe.objects.create(brand="BILSTEIN", sku_norm="19152615", oe_norm="OE1", oe_raw="OE1", source="api")
        PartOe.objects.create(brand="SACHS", sku_norm="315849", oe_norm="OE1", oe_raw="OE1", source="api")
        r = self.adm.post("/receive/preview/", {"supplier": self.sup.pk, "paste": "SACHS;315 849;1;800"})
        warns = r.context["plan"]["rows"][0]["warnings"]
        self.assertTrue(any("заміна" in w for w in warns))
        self.assertEqual(r.context["plan"]["rows"][0]["stock_qty"], 1)
        self.assertEqual(SaleItem.objects.get(pk=a.pk).fulfil_status, "ordered")
