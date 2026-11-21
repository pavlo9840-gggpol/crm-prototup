import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from core.models import Company, StaffMembership, Product, Supplier, Sale, StockMovement


class AcceptanceTest(TestCase):
    def test_flow(self):
        U = get_user_model()
        admin = U.objects.create_superuser("a", "", "x"); cashier = U.objects.create_user("c", password="x")
        from core.tests_tenant_helpers import create_test_company
        company = create_test_company(admin, "Acceptance shop", [(cashier, "cashier")])
        p = Product.objects.create(company=company, name="Кола", sku="K1", barcode="123", price=30, cost_price=20, min_stock=150)
        sup = Supplier.objects.create(company=company, name="S")
        adm, cash = self.client, self.client_class()
        adm.login(username="a", password="x"); cash.login(username="c", password="x")
        adm.post("/purchases/new/", {"supplier": sup.pk, "product": [p.pk], "qty": ["100"], "cost": ["20"]})
        p.refresh_from_db(); self.assertEqual(p.current_stock, 100)
        self.assertEqual(cash.get("/pos/").status_code, 302)
        cash.post("/shift/", {"action": "open", "cash": "0"})
        self.assertEqual(cash.get("/pos/").status_code, 200)
        self.assertEqual(cash.get("/pos/search/?q=123").json()["exact"], True)
        r = cash.post("/pos/checkout/", json.dumps({"items": [{"id": p.pk, "qty": 3, "price": 30}], "payment": "cash"}),
                      content_type="application/json")
        self.assertTrue(r.json()["ok"], r.content)
        p.refresh_from_db(); self.assertEqual(p.current_stock, 97)
        r = cash.post("/pos/checkout/", json.dumps({"items": [{"id": p.pk, "qty": 999}]}), content_type="application/json")
        self.assertEqual(r.status_code, 400); print(r.json()["error"])
        adm.post("/purchases/new/", {"supplier": sup.pk, "product": [p.pk], "qty": ["50"], "cost": ["20"]})
        adm.post("/inventory/adjust/", {"product": p.pk, "kind": "writeoff", "quantity": "2", "reason": "damaged"})
        p.refresh_from_db(); self.assertEqual(p.current_stock, 145)
        self.assertEqual(StockMovement.objects.filter(product=p).count(), 4)
        self.assertEqual(sum(m.quantity_change for m in StockMovement.objects.filter(product=p)), 145)
        sale = Sale.objects.get()
        adm.post(f"/sales/{sale.pk}/return/", {f"ret_{sale.items.first().pk}": "1"})
        p.refresh_from_db(); self.assertEqual(p.current_stock, 146)
        for url in ["/", "/products/", "/products/new/", f"/products/{p.pk}/edit/", "/categories/", "/suppliers/", "/purchases/",
                    "/purchases/new/", "/sales/", f"/sales/{sale.pk}/", "/inventory/", "/inventory/adjust/",
                    "/inventory/movements/", "/customers/", "/customers/new/", "/reports/daily/", "/reports/monthly/",
                    "/reports/top-products/", "/reports/profit/", "/reports/stock/", "/pos/held/", "/shift/"]:
            self.assertEqual(adm.get(url).status_code, 200, url)
        self.assertEqual(cash.get("/products/").status_code, 403)
        self.assertEqual(cash.get("/reports/daily/").status_code, 403)
        self.assertEqual(cash.get("/sales/").status_code, 200)
        r = cash.post("/pos/hold/", json.dumps({"items": [{"id": p.pk, "qty": 1}]}), content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertContains(adm.get("/reports/profit/"), "Кола")
        self.assertIn("Закрити", cash.get("/shift/").content.decode())
        self.assertContains(cash.post("/shift/", {"action": "close"}), "Зміну закрито")


class CashTest(TestCase):
    def setUp(self):
        from core import cash
        self.cash = cash
        U = get_user_model()
        self.owner = U.objects.create_superuser("a", "", "x")
        self.cashier = U.objects.create_user("c", password="x")
        from core.tests_tenant_helpers import create_test_company
        self.company = create_test_company(self.owner, "Cash test store", [(self.cashier, "cashier")])
        self.p = Product.objects.create(company=self.company, name="Кола", sku="K1", barcode="123", price=Decimal("30"), cost_price=20,
                                        current_stock=100, is_favorite=True)
        self.adm, self.cs = self.client, self.client_class()
        self.adm.login(username="a", password="x"); self.cs.login(username="c", password="x")

    def pay(self, **kw):
        body = {"items": [{"id": self.p.pk, "qty": 3, "price": 30}], **kw}
        return self.cs.post("/pos/checkout/", json.dumps(body), content_type="application/json")

    def test_change_plan_uses_drawer(self):
        # drawer: only 2x 20 and 1x 50 -> change 40 must be 2x20
        self.cs.post("/shift/", {"action": "open", "c_2000": "2", "c_5000": "1"})
        r = self.pay(payment="cash", received="130")        # 90 due -> change 40
        j = r.json(); self.assertTrue(j["ok"], j)
        self.assertEqual(j["change"], 40.0)
        self.assertEqual({p["l"]: p["n"] for p in j["plan"]}, {"20": 2})
        s = Sale.objects.get()
        self.assertEqual((s.cash_received, s.change_given, s.cash_amount), (Decimal("130"), Decimal("40"), Decimal("90")))
        from core.models import Shift
        sh = Shift.objects.get()
        self.assertEqual(self.cash.total(sh.drawer), 4000 + 5000 + 13000 - 4000)  # opening 2x20+50, plus received 100+20+10, minus change 2x20

    def test_no_change_available_blocks(self):
        self.cs.post("/shift/", {"action": "open", "c_10000": "1"})
        r = self.pay(payment="cash", received="200")        # change 110, drawer has only 100 (+ customer 200)
        self.assertEqual(r.status_code, 400); self.assertIn("розмінних", r.json()["error"])
        self.assertEqual(Sale.objects.count(), 0)

    def test_mixed_and_card(self):
        self.cs.post("/shift/", {"action": "open", "c_1000": "5"})
        j = self.pay(payment="mixed", card="50", received="50").json()    # 90 total: 50 card + 40 cash, received 50 -> change 10
        self.assertTrue(j["ok"], j); self.assertEqual(j["change"], 10.0)
        s = Sale.objects.get(); self.assertEqual((s.card_amount, s.cash_amount), (Decimal("50"), Decimal("40")))
        self.assertTrue(self.pay(payment="card").json()["ok"])

    def test_print_and_company_settings(self):
        self.cs.post("/shift/", {"action": "open"})
        sale = self.pay(payment="cash", received="90").json()
        r = self.cs.get(f"/print/receipt/?ids={sale['id']}")
        self.assertContains(r, "class=\"page\""); self.assertNotContains(r, "Адмін-панель")
        self.assertEqual(self.cs.get(f"/print/sale_invoice/?ids={sale['id']}").status_code, 403)
        self.assertEqual(self.cs.get("/designer/").status_code, 403)
        self.assertEqual(self.cs.get("/settings/company/").status_code, 403)
        self.assertEqual(self.cs.get(f"/sales/{sale['id']}/print/").status_code, 302)   # legacy URL redirects
        r = self.adm.post("/settings/company/", {"name": "Тест-Маркет", "receipt_footer": "Дякуємо", "auto_print": "on",
            "auto_print_format": "html", "vat_rate": "20",
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0", "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
            "form-0-bank_name": "ПриватБанк", "form-0-iban": "UA123", "form-0-currency": "UAH", "form-0-is_default": "on"})
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        from core.models import BankAccount, CompanySettings
        self.assertEqual(BankAccount.objects.count(), 1); self.assertEqual(CompanySettings.get().name, "Тест-Маркет")
        self.assertContains(self.adm.get(f"/print/sale_invoice/?ids={sale['id']}"), "Тест-Маркет")
        self.assertContains(self.adm.get(f"/print/sale_invoice/?ids={sale['id']}"), "у т.ч. ПДВ")


class DesignerTest(TestCase):
    def setUp(self):
        from core.models import PrintTemplate
        U = get_user_model()
        self.owner = U.objects.create_superuser("a", "", "x"); self.cashier = U.objects.create_user("c", password="x")
        from core.tests_tenant_helpers import create_test_company
        self.company = create_test_company(self.owner, "Designer test store", [(self.cashier, "cashier")])
        self.p = Product.objects.create(company=self.company, name="Кола", sku="K1", barcode="4820000000011", price=Decimal("30"), cost_price=20, current_stock=100)
        self.sup = Supplier.objects.create(company=self.company, name="S")
        self.adm, self.cs = self.client, self.client_class()
        self.adm.login(username="a", password="x"); self.cs.login(username="c", password="x")
        self.PT = PrintTemplate

    def _purchase(self, qty="10"):
        self.adm.post("/purchases/new/", {"supplier": self.sup.pk, "product": [self.p.pk], "qty": [qty], "cost": ["20"]})
        from core.models import Purchase
        return Purchase.objects.latest("pk")

    def test_pages_render(self):
        self.assertContains(self.adm.get("/designer/"), "Дизайнер бланків")
        n = self.PT.objects.filter(is_builtin=True).count(); self.assertEqual(n, 9)
        self.adm.get("/designer/")
        self.assertEqual(self.PT.objects.filter(is_builtin=True).count(), 9)          # idempotent
        for key in ("receipt", "sale_invoice", "purchase", "writeoff", "stock_report", "movement_report", "pricetag"):
            self.assertEqual(self.adm.get(f"/designer/select/?type={key}").status_code, 200)
        for t in self.PT.objects.all():
            self.assertEqual(self.adm.get(f"/designer/edit/{t.pk}/").status_code, 200, t.name)
            for fmt in ("html", "pdf", "png", "jpg"):
                r = self.adm.get(f"/designer/preview/{t.pk}/?fmt={fmt}")
                self.assertEqual(r.status_code, 200, (t.name, fmt))
            self.assertTrue(self.adm.get(f"/designer/preview/{t.pk}/?fmt=pdf").content.startswith(b"%PDF"))
        self.assertEqual(self.adm.get("/designer/font/regular/").status_code, 200)
        self.assertEqual(self.cs.get("/designer/font/bold/").status_code, 200)

    def test_custom_template_flow(self):
        self.adm.get("/designer/")
        std = self.PT.objects.get(builtin_key="sale_invoice-std")
        r = self.adm.post(f"/designer/copy/{std.pk}/"); self.assertEqual(r.status_code, 302)
        mine = self.PT.objects.filter(doc_type="sale_invoice", is_builtin=False).get()
        # builtin cannot be saved, custom can
        body = json.dumps({"name": "X", "layout": std.layout})
        self.assertEqual(self.adm.post(f"/designer/save/{std.pk}/", body, content_type="application/json").status_code, 403)
        lay = json.loads(json.dumps(mine.layout))
        lay["elements"].append({"id": "n1", "type": "text", "x": 10, "y": 280, "w": 100, "h": 6, "text": "МІЙ ШАБЛОН ТЕКСТ", "size": 10})
        r = self.adm.post(f"/designer/save/{mine.pk}/", json.dumps({"name": "Мій шаблон", "layout": lay}), content_type="application/json")
        self.assertTrue(r.json()["ok"], r.content)
        # print: default is the standard, chosen is mine
        self.cs.post("/shift/", {"action": "open"})
        from core.models import Sale
        sale = Sale.objects.create(company=self.company, cashier=get_user_model().objects.get(username="c"), status="done", total=30, cash_amount=30, number="CH-1")
        from core.models import SaleItem
        SaleItem.objects.create(sale=sale, product=self.p, quantity=1, original_price=30, price=30)
        r = self.adm.get(f"/print/sale_invoice/?ids={sale.pk}"); self.assertNotContains(r, "МІЙ ШАБЛОН ТЕКСТ")
        r = self.adm.get(f"/print/sale_invoice/?ids={sale.pk}&template={mine.pk}"); self.assertContains(r, "МІЙ ШАБЛОН ТЕКСТ")
        self.adm.post(f"/designer/default/{mine.pk}/")
        self.assertContains(self.adm.get(f"/print/sale_invoice/?ids={sale.pk}"), "МІЙ ШАБЛОН ТЕКСТ")
        # export -> import (copy, original untouched), default option
        exp = self.adm.get(f"/designer/export/{mine.pk}/"); data = json.loads(exp.content)
        self.assertEqual((data["kind"], data["doc_type"], data["name"]), ("print_template", "sale_invoice", "Мій шаблон"))
        from django.core.files.uploadedfile import SimpleUploadedFile
        before = self.PT.objects.count()
        r = self.adm.post("/designer/import/", {"file": SimpleUploadedFile("t.json", exp.content), "make_default": "on"})
        self.assertEqual(r.status_code, 302); self.assertEqual(self.PT.objects.count(), before + 1)
        imp = self.PT.objects.get(name="Мій шаблон (імпорт)"); self.assertTrue(imp.is_default)
        self.assertFalse(self.PT.objects.get(pk=mine.pk).is_default)
        # old-version file -> warning, still imported as copy
        data["layout"]["format_version"] = 99
        r = self.adm.post("/designer/import/", {"file": SimpleUploadedFile("t.json", json.dumps(data).encode())}, follow=True)
        self.assertContains(r, "несумісним")
        # garbage
        r = self.adm.post("/designer/import/", {"file": SimpleUploadedFile("t.json", b"not json")}, follow=True)
        self.assertContains(r, "Не вдалося")
        # delete custom ok, builtin forbidden
        self.assertEqual(self.adm.post(f"/designer/delete/{std.pk}/").status_code, 403)
        self.assertEqual(self.adm.post(f"/designer/delete/{imp.pk}/").status_code, 302)
        self.assertFalse(self.PT.objects.filter(pk=imp.pk).exists())

    def test_user_templates_survive_update(self):
        self.adm.get("/designer/")
        std = self.PT.objects.get(builtin_key="purchase-std")
        self.adm.post(f"/designer/copy/{std.pk}/")
        mine = self.PT.objects.get(doc_type="purchase", is_builtin=False)
        # simulate an "update" that changes the builtin layout: builtin is refreshed, custom untouched
        std.layout = {"format_version": 1, "page": {"preset": "a4", "w": 210, "h": 297, "auto_height": False}, "elements": []}
        std.save(); mine_layout = json.loads(json.dumps(mine.layout))
        self.adm.get("/designer/")
        std.refresh_from_db(); mine.refresh_from_db()
        self.assertTrue(len(std.layout["elements"]) > 3); self.assertEqual(mine.layout, mine_layout)

    def test_sanitize_rejects_bad_input(self):
        from core import designer
        c, w = designer.sanitize({"page": {"preset": ["x"], "w": "abc"}, "elements": [
            {"type": "evil"}, {"type": "image", "src": "javascript:alert(1)"}, {"type": "text", "x": "nan", "w": -5, "text": "t"},
            {"type": "field", "field": "doc.nope"}, "junk"]}, "receipt")
        self.assertEqual([e["type"] for e in c["elements"]], ["image", "text", "field"])
        self.assertEqual(c["elements"][0]["src"], ""); self.assertTrue(w)
        with self.assertRaises(ValueError): designer.sanitize({"elements": "x"}, "receipt")
        r = self.adm.post(f"/designer/save/{self.PT.objects.create(doc_type='receipt', name='t', layout={}).pk}/",
                          json.dumps({"name": "t", "layout": {"elements": "bad"}}), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_conditions_flow_and_pagination(self):
        from core import designer, docs
        ctx = docs.sample_ctx("sale_invoice")
        el = lambda **k: {"id": k.pop("id"), "type": "field", "x": 10, "y": 10, "w": 100, "h": 6, "field": "doc.customer", **k}
        lay, _ = designer.sanitize({"page": {"preset": "a4", "w": 210, "h": 297, "auto_height": False}, "elements": [
            el(id="a", show_if={"field": "doc.total", "op": "gt", "value": "100"}, prefix="BIG"),
            el(id="b", y=20, show_if={"field": "doc.total", "op": "lt", "value": "100"}, prefix="SMALL"),
            el(id="c", y=30, empty="dash", field="doc.note_none", prefix="N:"),
            {"id": "d", "type": "field", "x": 10, "y": 40, "w": 100, "h": 6, "field": "doc.total", "bold_if": {"field": "doc.total", "op": "ge", "value": "300"}}]}, "sale_invoice")
        pg = designer.resolve(lay, ctx)[0]
        texts = [p for p in pg["prims"] if p["t"] == "text"]
        joined = " | ".join(l for p in texts for l in p["lines"])
        self.assertIn("BIG", joined); self.assertNotIn("SMALL", joined); self.assertIn("N:—", joined)
        self.assertTrue([p for p in texts if p["lines"] == [ctx["doc"]["total"]]][0]["bold"])
        # flow: hidden flow element pulls following one up
        lay2, _ = designer.sanitize({"page": {"preset": "custom", "w": 80, "h": 100, "auto_height": True}, "elements": [
            {"id": "h", "type": "text", "x": 0, "y": 0, "w": 50, "h": 10, "text": "", "flow": True, "size": 9},
            {"id": "k", "type": "text", "x": 0, "y": 12, "w": 50, "h": 5, "text": "KEEP", "size": 9}]}, "receipt")
        pg2 = designer.resolve(lay2, ctx)[0]; k = [p for p in pg2["prims"] if p["lines"] == ["KEEP"]][0]
        self.assertAlmostEqual(k["y"], 2.0, places=1)
        # pagination: 150 rows on fixed A4 -> several pages, header repeats, footer on the last
        from core.builtin_templates import sale_invoice
        lay3, _ = designer.sanitize(sale_invoice(False), "sale_invoice")
        ctx["items"] = [dict(ctx["items"][0], n=str(i)) for i in range(1, 151)]
        pages = designer.resolve(lay3, ctx); self.assertGreater(len(pages), 2)
        for pg in pages: self.assertAlmostEqual(pg["h"], 297.0, places=0)
        self.assertIn("Разом:", " ".join(l for p in pages[-1]["prims"] if p["t"] == "text" for l in p["lines"]))
        self.assertNotIn("Разом:", " ".join(l for p in pages[0]["prims"] if p["t"] == "text" for l in p["lines"]))
        for pg in pages[1:]:
            self.assertIn("Назва", " ".join(l for p in pg["prims"] if p["t"] == "text" for l in p["lines"]))

    def test_barcode_qr_and_image_outputs(self):
        from core import designer, docs
        import base64, io
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 20), "red").save(buf, "PNG")
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        lay, _ = designer.sanitize({"page": {"preset": "tag58", "w": 58, "h": 40, "auto_height": False}, "elements": [
            {"id": "i", "type": "image", "x": 1, "y": 1, "w": 20, "h": 10, "src": uri},
            {"id": "b", "type": "barcode", "x": 2, "y": 12, "w": 50, "h": 12, "field": "doc.barcode"},
            {"id": "q", "type": "qr", "x": 2, "y": 26, "w": 12, "h": 12, "static": "https://example.com"},
            {"id": "b2", "type": "barcode", "x": 2, "y": 1, "w": 10, "h": 5, "static": "Кирилиця"}]}, "pricetag")
        pg = designer.resolve(lay, docs.sample_ctx("pricetag"))[0]
        kinds = [p["t"] for p in pg["prims"]]
        self.assertEqual(kinds.count("fill"), 2); self.assertIn("img", kinds)       # cyrillic barcode skipped
        html = designer.render_page_html(pg); self.assertIn("<svg", html); self.assertIn("data:image/png", html)
        pdf = designer.render_pdf([pg]); self.assertTrue(pdf.startswith(b"%PDF"))
        png = designer.render_images(pdf, "png")[0]; self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(designer.render_images(pdf, "jpg")[0][:2] == b"\xff\xd8")

    def test_real_documents_and_group_print(self):
        pur1, pur2 = self._purchase("10"), self._purchase("5")
        r = self.adm.get(f"/print/purchase/?ids={pur1.pk}&ids={pur2.pk}"); self.assertContains(r, "NK-"); self.assertContains(r, "Сторінок: 2")
        self.assertTrue(self.adm.get(f"/print/purchase/?ids={pur1.pk},{pur2.pk}&fmt=pdf").content.startswith(b"%PDF"))
        r = self.adm.get(f"/print/purchase/?ids={pur1.pk},{pur2.pk}&fmt=png"); self.assertEqual(r["Content-Type"], "application/zip")
        self.assertEqual(self.adm.get("/print/purchase/?ids=99999").status_code, 404)
        self.assertEqual(self.adm.get("/print/purchase/").status_code, 404)
        self.assertEqual(self.adm.get("/print/unknown/?ids=1").status_code, 404)
        self.assertEqual(self.cs.get(f"/print/purchase/?ids={pur1.pk}").status_code, 403)
        # tags (2 copies of 1 product) / reports / acts
        r = self.adm.get(f"/print/pricetag/?ids={self.p.pk}&copies=2"); self.assertContains(r, "Сторінок: 2")
        self.assertContains(self.adm.get("/print/stock_report/"), "Кола")
        self.assertContains(self.adm.get("/print/movement_report/?from=2020-01-01"), "NK-")
        self.adm.post("/inventory/adjust/", {"product": self.p.pk, "kind": "writeoff", "quantity": "2", "reason": "damaged"})
        from core.models import StockAdjustment
        a = StockAdjustment.objects.get(); self.assertContains(self.adm.get(f"/print/writeoff/?ids={a.pk}"), "ADJ-")
        self.assertContains(self.adm.get("/inventory/adjustments/"), "ADJ-")
        # list pages with group-print bar and sidebar groups
        for url in ("/sales/", "/purchases/", "/products/", "/inventory/", "/inventory/movements/", "/inventory/adjustments/"):
            r = self.adm.get(url); self.assertEqual(r.status_code, 200, url)
        self.assertContains(self.adm.get("/products/"), "Друк вибраних")
        r = self.adm.get("/products/"); self.assertContains(r, 'class="grp'); self.assertContains(r, "data-g=\"Склад\"")


class HardeningTest(DesignerTest):
    """Re-uses DesignerTest fixtures; checks fixes found during code review."""

    def test_txt_receipt_and_zip_backup(self):
        self.cs.post("/shift/", {"action": "open"})
        from core.models import Sale, SaleItem
        sale = Sale.objects.create(cashier=get_user_model().objects.get(username="c"), status="done", total=30, cash_amount=30,
                                   cash_received=50, change_given=20, number="CH-7")
        SaleItem.objects.create(sale=sale, product=self.p, quantity=1, original_price=30, price=30)
        r = self.cs.get(f"/print/receipt/?ids={sale.pk}&fmt=txt&cols=32")
        self.assertEqual(r["Content-Type"], "text/plain; charset=utf-8")
        txt = r.content.decode(); self.assertIn("Чек № CH-7", txt); self.assertIn("Здача: 20.00", txt)
        self.assertTrue(all(len(l) <= 32 for l in txt.splitlines()), [l for l in txt.splitlines() if len(l) > 32])
        # zip backup -> import
        self.adm.get("/designer/")
        std = self.PT.objects.get(builtin_key="receipt-58"); self.adm.post(f"/designer/copy/{std.pk}/")
        self.adm.post(f"/designer/copy/{self.PT.objects.get(builtin_key='purchase-std').pk}/")
        z = self.adm.get("/designer/export-all/"); self.assertEqual(z["Content-Type"], "application/zip")
        import zipfile, io
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(z.content)).namelist()), 2)
        from django.core.files.uploadedfile import SimpleUploadedFile
        n = self.PT.objects.count()
        r = self.adm.post("/designer/import/", {"file": SimpleUploadedFile("b.zip", z.content)}, follow=True)
        self.assertEqual(self.PT.objects.count(), n + 2); self.assertContains(r, "Завантажено шаблонів: 2")
        r = self.adm.post("/designer/import/", {"file": SimpleUploadedFile("b.zip", b"not a zip")}, follow=True)
        self.assertContains(r, "ZIP")

    def test_corrupted_template_does_not_crash(self):
        bad = self.PT.objects.create(doc_type="receipt", name="bad", layout={"elements": "oops"})
        self.assertEqual(self.adm.get(f"/designer/edit/{bad.pk}/").status_code, 404)
        self.cs.post("/shift/", {"action": "open"})
        from core.models import Sale, SaleItem
        sale = Sale.objects.create(cashier=get_user_model().objects.get(username="c"), status="done", total=30, cash_amount=30, number="CH-8")
        SaleItem.objects.create(sale=sale, product=self.p, quantity=1, original_price=30, price=30)
        self.assertEqual(self.cs.get(f"/print/receipt/?ids={sale.pk}&template={bad.pk}").status_code, 400)

    def test_purchase_rejects_invalid_line_without_partial_post_or_orphan_supplier(self):
        from core.models import Purchase as PurchaseModel
        product = Product.objects.create(name="Фільтр", sku="F-1", cost_price=Decimal("10"), current_stock=5)
        supplier = Supplier.objects.create(name="Постачальник")
        purchase_count = PurchaseModel.objects.count()
        movement_count = StockMovement.objects.count()
        response = self.adm.post("/purchases/new/", {
            "new_supplier": "Не має створитися",
            "product": [str(product.pk), str(product.pk)],
            "qty": ["2", "1"],
            "cost": ["10", "-1"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "закупівельна ціна")
        self.assertEqual(PurchaseModel.objects.count(), purchase_count)
        self.assertEqual(Supplier.objects.filter(name="Не має створитися").count(), 0)
        self.assertEqual(StockMovement.objects.count(), movement_count)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("5"))

    def test_bad_json_and_empty_purchase(self):
        self.cs.post("/shift/", {"action": "open"})
        for url in ("/pos/checkout/", "/pos/hold/", "/pos/change/", "/pos/customer/new/"):
            r = self.cs.post(url, "{not json", content_type="application/json")
            self.assertIn(r.status_code, (200, 400), url)
        from core.models import Supplier as S
        n = S.objects.count()
        r = self.adm.post("/purchases/new/", {"new_supplier": "Ghost", "product": [""], "qty": [""], "cost": [""]})
        self.assertEqual(S.objects.count(), n); self.assertContains(r, "Додайте хоча б один товар")


class V4Test(TestCase):
    def setUp(self):
        from core.models import PrintTemplate
        U = get_user_model()
        self.admin = U.objects.create_superuser("a", "", "x"); self.cashier = U.objects.create_user("c", password="x")
        from core.tests_tenant_helpers import create_test_company
        self.company = create_test_company(self.admin, "V4 test store", [(self.cashier, "cashier")])
        self.adm, self.cs = self.client, self.client_class()
        self.adm.login(username="a", password="x"); self.cs.login(username="c", password="x")
        self.PT = PrintTemplate

    def test_purchase_list_search_supplier_and_filtered_totals(self):
        from decimal import Decimal
        from core.models import Product, Purchase, Supplier
        from core.services import create_purchase

        supplier_a = Supplier.objects.create(company=self.company, name="Фільтри Захід")
        supplier_b = Supplier.objects.create(company=self.company, name="АвтоСвіт")
        product = Product.objects.create(company=self.company, name="Масляний фільтр", sku="OF-22", cost_price=Decimal("12.00"))
        create_purchase(self.admin, supplier_a, [(product, Decimal("2"), Decimal("12.00"))])
        create_purchase(self.admin, supplier_b, [(product, Decimal("1"), Decimal("12.00"))])

        response = self.adm.get("/purchases/?q=Фільтри")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Фільтри Захід")
        self.assertContains(response, "Знайдено накладних: <strong>1</strong>")
        self.assertContains(response, "24.00 ₴")
        self.assertContains(response, 'value="Фільтри"')

        response = self.adm.get(f"/purchases/?supplier={supplier_b.pk}")
        self.assertContains(response, "АвтоСвіт")
        self.assertContains(response, "12.00 ₴")
        self.assertContains(response, f'value="{supplier_b.pk}" selected')

        response = self.adm.get("/purchases/?q=немає-такого")
        self.assertContains(response, "Накладних за вибраними умовами не знайдено")
        self.assertContains(response, "Знайдено накладних: <strong>0</strong>")

    # ---- legacy templates -> designer
    def test_legacy_conversion(self):
        from core.models import DocTemplate
        old = DocTemplate.objects.create(kind="receipt", paper="80mm", body=(
            '<div class="c"><b>{{ company.name }}</b><br>{% if company.phone %}Тел.: {{ company.phone }}<br>{% endif %}</div><hr>'
            'Чек № <b>{{ doc.number }}</b><br>Касир: {{ doc.user }}<hr><table>{% for i in items %}<tr><td>{{ i.name }}</td>'
            '<td>{{ i.qty }}</td><td class="r">{{ i.total }}</td></tr>{% endfor %}</table><div class="tot">РАЗОМ: {{ doc.total }}</div>'))
        DocTemplate.objects.create(kind="purchase", paper="a4", body="<h2>Накладна {{ doc.number }}</h2>{% for i in items %}{{ i.name }}{% endfor %}")
        r = self.adm.get("/designer/"); self.assertContains(r, "Старі шаблони"); self.assertContains(r, "Перетворити на новий")
        r = self.adm.post(f"/designer/legacy/{old.pk}/convert/"); self.assertEqual(r.status_code, 302)
        t = self.PT.objects.get(name__contains="зі старої версії"); self.assertEqual(t.doc_type, "receipt")
        types = [e["type"] for e in t.layout["elements"]]; self.assertIn("table", types); self.assertIn("line", types)
        texts = [e["text"] for e in t.layout["elements"] if e["type"] == "text"]
        self.assertIn("Чек № {{doc.number}}", texts); self.assertIn("Касир: {{doc.cashier}}", texts)   # doc.user remapped
        phone = [e for e in t.layout["elements"] if e["type"] == "text" and "phone" in e["text"]][0]
        self.assertEqual(phone["show_if"]["field"], "company.phone")                                   # {% if %} -> condition
        # converted template prints real documents with substituted tokens
        from core import designer, docs
        pg = designer.resolve(designer.sanitize(t.layout, "receipt")[0], docs.sample_ctx("receipt"))[0]
        joined = " ".join(l for p in pg["prims"] if p["t"] == "text" for l in p["lines"])
        self.assertIn("Чек № CH-000123", joined); self.assertNotIn("{{", joined)
        self.assertTrue(designer.render_pdf([pg]).startswith(b"%PDF"))
        self.assertEqual(self.adm.get(f"/designer/edit/{t.pk}/").status_code, 200)
        self.adm.post(f"/designer/legacy/{old.pk}/delete/"); self.assertEqual(DocTemplate.objects.count(), 1)
        self.adm.post("/designer/legacy/clear/"); self.assertEqual(DocTemplate.objects.count(), 0)
        self.assertNotContains(self.adm.get("/designer/"), "Старі шаблони")
        self.assertEqual(self.cs.post("/designer/legacy/clear/").status_code, 403)

    def test_text_tokens(self):
        from core import designer, docs
        lay, w = designer.sanitize({"page": {"preset": "a4", "w": 210, "h": 100, "auto_height": False}, "elements": [
            {"id": "t", "type": "text", "x": 1, "y": 1, "w": 100, "h": 6, "text": "№ {{doc.number}} / {{doc.nope}}"}]}, "receipt")
        self.assertTrue(any("doc.nope" in x for x in w))
        pg = designer.resolve(lay, docs.sample_ctx("receipt"))[0]
        self.assertEqual(pg["prims"][0]["lines"], ["№ CH-000123 / "])

    # ---- users
    def test_user_management(self):
        U = get_user_model()
        self.assertEqual(self.cs.get("/users/").status_code, 403)
        self.assertContains(self.adm.get("/users/"), "Користувачі")
        r = self.adm.post("/users/new/", {"username": "ola", "role": "cashier", "is_active": "on", "password": "secret1", "first_name": "Оля"})
        self.assertEqual(r.status_code, 302); u = U.objects.get(username="ola")
        self.assertFalse(u.is_staff or u.is_superuser); self.assertTrue(u.check_password("secret1"))
        self.assertContains(self.adm.post("/users/new/", {"username": "OLA", "role": "cashier", "password": "secret1"}), "уже існує")
        self.assertContains(self.adm.post("/users/new/", {"username": "x1", "role": "cashier", "password": "12"}), "занадто короткий")
        self.assertContains(self.adm.post("/users/new/", {"username": "x2", "role": "cashier"}), "Вкажіть пароль")
        c = self.client_class(); self.assertTrue(c.login(username="ola", password="secret1"))
        self.assertEqual(c.get("/", follow=True).redirect_chain[-1][0], "/shift/")        # cashier lands in POS flow
        # edit: promote, change password, deactivate
        self.adm.post(f"/users/{u.pk}/edit/", {"username": "ola", "role": "admin", "is_active": "on", "password": ""})
        u.refresh_from_db(); self.assertTrue(u.is_superuser and u.is_staff and u.check_password("secret1"))
        self.adm.post(f"/users/{u.pk}/edit/", {"username": "ola", "role": "cashier", "password": "newpass1"})
        u.refresh_from_db(); self.assertFalse(u.is_active); self.assertTrue(u.check_password("newpass1")); self.assertFalse(u.is_superuser)
        self.assertFalse(self.client_class().login(username="ola", password="newpass1"))
        # cannot change own role / lock out last admin
        self.adm.post(f"/users/{self.admin.pk}/edit/", {"username": "a", "role": "cashier", "password": ""})
        self.admin.refresh_from_db(); self.assertTrue(self.admin.is_superuser and self.admin.is_active)
        other = U.objects.create_superuser("b", "", "x"); self.adm.post(f"/users/{other.pk}/edit/", {"username": "b", "role": "cashier"})
        other.refresh_from_db(); self.assertFalse(other.is_superuser)       # allowed: another admin (a) remains
        b = self.client_class(); b.login(username="a", password="x")
        U.objects.filter(pk=u.pk).update(is_active=True)

    # ---- CSV
    def _csv(self, text, enc="utf-8-sig"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return {"file": SimpleUploadedFile("p.csv", text.encode(enc))}

    def test_csv_export_import(self):
        from core.models import PriceHistory, StockMovement, Category
        cat = Category.objects.create(company=self.company, name="Напої")
        p = Product.objects.create(company=self.company, name="Кола", sku="COLA", barcode="111", price=30, cost_price=20, category=cat, unit="пляшка")
        csv_ = self.adm.get("/products/export/").content.decode("utf-8-sig")
        self.assertIn("Код;Назва;Штрих-код", csv_); self.assertIn("COLA;Кола;111;Напої;пляшка;20.00;30.00", csv_)
        self.assertIn("COLA05", self.adm.get("/products/export/?template=1").content.decode("utf-8-sig"))
        data = ("Код;Назва;Штрих-код;Категорія;Ціна продажу;Залишок;Активний\n"
                "COLA;;;;35,50;999;\n"                           # update price only; stock ignored for existing
                "NEW1;Печиво;222;Їжа → Солодке;12,5;40;так\n"      # create + category path + opening stock
                "NEW2;Дубль;111;;5;0;1\n"                         # barcode owned by COLA
                "NEW3;;;;5;0;1\n"                                 # new without name
                "NEW1;Повтор;;;5;0;1\n"                           # duplicate sku in file
                "NEW4;Погано;;;abc;0;1\n"                         # bad number
                "NEW5;Ок;;;7;;ні\n")
        r = self.adm.post("/products/import/", self._csv(data))
        self.assertContains(r, "Буде створено"); self.assertContains(r, "штрих-код 111 вже належить"); self.assertContains(r, "потрібна назва")
        self.assertContains(r, "повторюється у файлі"); self.assertContains(r, "некоректне число")
        self.assertEqual(Product.objects.count(), 1)                    # nothing saved before confirmation
        r = self.adm.post("/products/import/apply/", follow=True); self.assertContains(r, "створено 2, оновлено 1")
        p.refresh_from_db(); self.assertEqual(p.price, Decimal("35.50")); self.assertEqual(p.current_stock, 0)
        self.assertEqual(PriceHistory.objects.filter(product=p, field="price").count(), 1)
        n1 = Product.objects.get(sku="NEW1"); self.assertEqual(n1.current_stock, 40); self.assertEqual(str(n1.category), "Їжа → Солодке")
        self.assertEqual(StockMovement.objects.filter(product=n1, movement_type="opening").count(), 1)
        self.assertFalse(Product.objects.get(sku="NEW5").is_active)
        self.assertEqual(self.adm.post("/products/import/apply/", follow=True).status_code, 200)   # no data -> message, no crash
        # cp1251 + comma delimiter; missing sku column
        r = self.adm.post("/products/import/", self._csv("sku,name,price\nCP1,Тест,10\n", "cp1251")); self.assertContains(r, "Буде створено")
        r = self.adm.post("/products/import/", self._csv("name,price\nX,1\n"), follow=True); self.assertContains(r, "Не знайдено колонки")
        self.assertEqual(self.cs.get("/products/import/").status_code, 403)
        self.assertEqual(self.cs.get("/products/export/").status_code, 403)

    def test_new_pages_and_nav(self):
        for url in ("/users/", "/users/new/", f"/users/{self.admin.pk}/edit/", "/backup/", "/products/import/", "/products/", "/designer/"):
            self.assertEqual(self.adm.get(url).status_code, 200, url)
        r = self.adm.get("/users/"); self.assertContains(r, 'data-g="Налаштування"'); self.assertContains(r, "Резервні копії")


from django.test import TransactionTestCase


class BackupTest(TransactionTestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("a", "", "x"); U.objects.create_user("c", password="x")
        self.adm, self.cs = self.client, self.client_class()
        self.adm.login(username="a", password="x"); self.cs.login(username="c", password="x")

    def test_backup(self):
        import tempfile, zipfile, io, sqlite3
        from pathlib import Path
        from unittest import mock
        from core import admin_tools
        Product.objects.create(name="P", sku="S1", price=1)
        r = self.adm.post("/backup/", {"action": "download"}); self.assertEqual(r["Content-Type"], "application/zip")
        z = zipfile.ZipFile(io.BytesIO(r.content)); self.assertIn("db.sqlite3", z.namelist()); self.assertIn("README.txt", z.namelist())
        with tempfile.TemporaryDirectory() as td:
            Path(td, "x.sqlite3").write_bytes(z.read("db.sqlite3"))
            con = sqlite3.connect(Path(td, "x.sqlite3")); self.assertEqual(con.execute("select count(*) from core_product").fetchone()[0], 1); con.close()
            with mock.patch.object(admin_tools, "BACKUP_DIR", Path(td) / "backups"):
                self.assertEqual(self.adm.post("/backup/", {"action": "save"}).status_code, 302)
                name = next((Path(td) / "backups").glob("backup-*.zip")).name
                self.assertContains(self.adm.get("/backup/"), name)
                self.assertEqual(self.adm.get(f"/backup/download/{name}/").status_code, 200)
                self.assertEqual(self.adm.get("/backup/download/..%2Fetc%2Fpasswd/").status_code, 404)
                self.assertEqual(self.adm.get("/backup/download/evil.zip/").status_code, 404)
        self.assertEqual(self.cs.get("/backup/").status_code, 403)



class FinanceOverviewReportTest(TestCase):
    def setUp(self):
        from core.models import FinanceEntry
        U = get_user_model()
        self.user = U.objects.create_superuser("finadmin", "", "x")
        self.company = Company.objects.create(name="Finance test", slug="finance-test", owner=self.user)
        StaffMembership.objects.create(company=self.company, user=self.user, role="owner")
        self.client.login(username="finadmin", password="x")
        self.FinanceEntry = FinanceEntry

    def test_report_is_available_and_tenant_scoped(self):
        from datetime import date
        from core.models import Product, Sale, SaleItem
        from django.utils import timezone
        from decimal import Decimal
        product = Product.objects.create(company=self.company, name="Test item", sku="FIN-1", price=150, cost_price=90)
        sale = Sale.objects.create(company=self.company, cashier=self.user, total=Decimal("300.00"), status=Sale.DONE)
        SaleItem.objects.create(company=self.company, sale=sale, product=product, quantity=2,
                                original_price=150, price=150, cost_price=90, earned_at=timezone.now())
        self.FinanceEntry.objects.create(company=self.company, entry_type="income", category="Інше",
                                         amount=Decimal("50.00"), created_by=self.user)
        self.FinanceEntry.objects.create(company=self.company, entry_type="expense", category="Оренда",
                                         amount=Decimal("30.00"), created_by=self.user)
        response = self.client.get("/reports/finance/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["revenue"], Decimal("300.00"))
        self.assertEqual(response.context["cost"], Decimal("180.00"))
        self.assertEqual(response.context["gross_profit"], Decimal("120.00"))
        self.assertEqual(response.context["other_income"], Decimal("50.00"))
        self.assertEqual(response.context["expenses"], Decimal("30.00"))
        self.assertEqual(response.context["operating_result"], Decimal("140.00"))
        # A second tenant's sale must never leak into this report.
        other = Company.objects.create(name="Other tenant", slug="finance-other", owner=self.user)
        other_product = Product.objects.create(company=other, name="Other item", sku="FIN-OTHER", price=999, cost_price=500)
        other_sale = Sale.objects.create(company=other, cashier=self.user, total=Decimal("999.00"), status=Sale.DONE)
        SaleItem.objects.create(company=other, sale=other_sale, product=other_product, quantity=1,
                                original_price=999, price=999, cost_price=500, earned_at=timezone.now())
        response = self.client.get("/reports/finance/")
        self.assertEqual(response.context["revenue"], Decimal("300.00"))

    def test_report_csv_export_and_formula_safe_category(self):
        self.FinanceEntry.objects.create(company=self.company, entry_type="expense", category="=CMD()",
                                         amount=Decimal("10.00"), created_by=self.user)
        response = self.client.get("/reports/finance/?export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"'=CMD()", response.content)

    def test_settlements_and_advances_do_not_double_count_profit(self):
        from decimal import Decimal
        from django.utils import timezone
        from core.models import (
            AccountAdvance, BankAccount, BankTransaction, Customer, Product, Purchase,
            Sale, SaleItem, SettlementPayment, Supplier,
        )

        customer = Customer.objects.create(company=self.company, name="Finance customer", phone="+380501111111")
        supplier = Supplier.objects.create(company=self.company, name="Finance supplier")
        product = Product.objects.create(company=self.company, name="Paid item", sku="FIN-PAID",
                                         price=100, cost_price=40)
        sale = Sale.objects.create(company=self.company, cashier=self.user, customer=customer,
                                   total=Decimal("100.00"), status=Sale.DONE,
                                   cash_amount=Decimal("60.00"), card_amount=Decimal("40.00"))
        SaleItem.objects.create(company=self.company, sale=sale, product=product, quantity=1,
                                 original_price=100, price=100, cost_price=40, earned_at=timezone.now())
        purchase = Purchase.objects.create(company=self.company, supplier=supplier, user=self.user,
                                            total=Decimal("50.00"))
        SettlementPayment.objects.create(company=self.company, sale=sale, amount=Decimal("20.00"),
                                          payment_method="bank", created_by=self.user)
        SettlementPayment.objects.create(company=self.company, purchase=purchase, amount=Decimal("25.00"),
                                          payment_method="bank", created_by=self.user)
        AccountAdvance.objects.create(company=self.company, party_type="customer", customer=customer,
                                      amount=Decimal("30.00"), remaining=Decimal("30.00"),
                                      payment_method="bank", created_by=self.user)
        AccountAdvance.objects.create(company=self.company, party_type="supplier", supplier=supplier,
                                      amount=Decimal("15.00"), remaining=Decimal("15.00"),
                                      payment_method="bank", created_by=self.user)
        account = BankAccount.objects.create(company=self.company, bank_name="Test bank",
                                             iban="UA123456789012345678901234567")
        BankTransaction.objects.create(company=self.company, account=account, transaction_date=timezone.localdate(),
                                       direction=BankTransaction.INCOME, category="Оплата клієнта",
                                       amount=Decimal("20.00"), created_by=self.user)

        response = self.client.get("/reports/finance/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["revenue"], Decimal("100.00"))
        self.assertEqual(response.context["cost"], Decimal("40.00"))
        self.assertEqual(response.context["gross_profit"], Decimal("60.00"))
        self.assertEqual(response.context["other_income"], Decimal("0.00"))
        self.assertEqual(response.context["expenses"], Decimal("0.00"))
        self.assertEqual(response.context["operating_result"], Decimal("60.00"))
        self.assertEqual(response.context["bank_in"], Decimal("20.00"))

    def test_cash_withdrawals_are_reported_as_positive_outflows(self):
        from decimal import Decimal
        from core.models import CashOperation, Shift

        shift = Shift.objects.create(company=self.company, cashier=self.user)
        CashOperation.objects.create(company=self.company, shift=shift, user=self.user,
                                     kind="out", amount=Decimal("-25.00"), note="Вилучення")
        CashOperation.objects.create(company=self.company, shift=shift, user=self.user,
                                     kind="in", amount=Decimal("10.00"), note="Внесення")
        response = self.client.get("/reports/finance/")
        self.assertEqual(response.context["cash_in"], Decimal("10.00"))
        self.assertEqual(response.context["cash_out"], Decimal("25.00"))

    def test_returns_are_recognized_in_the_period_they_are_processed(self):
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        from core.models import Product, Sale, SaleItem, SaleReturn, SaleReturnItem

        product = Product.objects.create(company=self.company, name="Returned item", sku="FIN-RET",
                                         price=150, cost_price=90)
        old_sale = Sale.objects.create(company=self.company, cashier=self.user, total=Decimal("150.00"),
                                       status=Sale.DONE)
        old_earned_at = timezone.now() - timedelta(days=40)
        item = SaleItem.objects.create(company=self.company, sale=old_sale, product=product, quantity=1,
                                       original_price=150, price=150, cost_price=90,
                                       returned_qty=1, earned_at=old_earned_at)
        sale_return = SaleReturn.objects.create(company=self.company, sale=old_sale, user=self.user,
                                                total=Decimal("150.00"), refund_method="card",
                                                card_amount=Decimal("150.00"))
        SaleReturnItem.objects.create(company=self.company, sale_return=sale_return, sale_item=item,
                                      quantity=1, price=Decimal("150.00"))

        response = self.client.get("/reports/finance/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["gross_sales"], Decimal("0.00"))
        self.assertEqual(response.context["returned_revenue"], Decimal("150.00"))
        self.assertEqual(response.context["returned_cost"], Decimal("90.00"))
        self.assertEqual(response.context["revenue"], Decimal("-150.00"))
        self.assertEqual(response.context["cost"], Decimal("-90.00"))
        self.assertEqual(response.context["gross_profit"], Decimal("-60.00"))


class FinanceControlsReportTest(TestCase):
    def setUp(self):
        from core.models import Company, StaffMembership
        U = get_user_model()
        self.user = U.objects.create_superuser("controlsadmin", "", "x")
        self.company = Company.objects.create(name="Controls test", slug="controls-test", owner=self.user)
        StaffMembership.objects.create(company=self.company, user=self.user, role="owner")
        self.client.login(username="controlsadmin", password="x")

    def test_controls_report_shows_debts_advances_and_unmatched_bank_items(self):
        from decimal import Decimal
        from django.utils import timezone
        from core.models import (
            AccountAdvance, BankAccount, BankReconciliation, BankTransaction,
            Customer, Purchase, Sale, SettlementPayment, Supplier,
        )
        customer = Customer.objects.create(company=self.company, name="Control customer", phone="+380501234567")
        supplier = Supplier.objects.create(company=self.company, name="Control supplier")
        sale = Sale.objects.create(company=self.company, cashier=self.user, customer=customer,
                                   total=Decimal("200.00"), cash_amount=Decimal("50.00"),
                                   status=Sale.DONE)
        purchase = Purchase.objects.create(company=self.company, supplier=supplier, user=self.user,
                                           total=Decimal("300.00"))
        customer_payment = SettlementPayment.objects.create(
            company=self.company, sale=sale, amount=Decimal("15.00"),
            payment_method="bank", created_by=self.user,
        )
        supplier_payment = SettlementPayment.objects.create(
            company=self.company, purchase=purchase, amount=Decimal("20.00"),
            payment_method="bank", created_by=self.user,
        )
        account = BankAccount.objects.create(company=self.company, bank_name="Control bank",
                                             iban="UA123456789012345678901234567")
        matched_in = BankTransaction.objects.create(
            company=self.company, account=account, transaction_date=timezone.localdate(),
            direction=BankTransaction.INCOME, category="Оплата клієнта",
            amount=Decimal("15.00"), created_by=self.user,
        )
        matched_out = BankTransaction.objects.create(
            company=self.company, account=account, transaction_date=timezone.localdate(),
            direction=BankTransaction.EXPENSE, category="Оплата постачальнику",
            amount=Decimal("20.00"), created_by=self.user,
        )
        BankReconciliation.objects.create(company=self.company, bank_transaction=matched_in,
                                           settlement_payment=customer_payment, matched_by=self.user)
        BankReconciliation.objects.create(company=self.company, bank_transaction=matched_out,
                                           settlement_payment=supplier_payment, matched_by=self.user)
        BankTransaction.objects.create(
            company=self.company, account=account, transaction_date=timezone.localdate(),
            direction=BankTransaction.INCOME, category="Невідоме надходження",
            amount=Decimal("30.00"), created_by=self.user,
        )
        AccountAdvance.objects.create(company=self.company, party_type=AccountAdvance.CUSTOMER,
                                      customer=customer, amount=Decimal("25.00"), remaining=Decimal("25.00"),
                                      payment_method="bank", created_by=self.user)
        AccountAdvance.objects.create(company=self.company, party_type=AccountAdvance.SUPPLIER,
                                      supplier=supplier, amount=Decimal("35.00"), remaining=Decimal("35.00"),
                                      payment_method="bank", created_by=self.user)

        response = self.client.get("/reports/finance/controls/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["customer_debt_total"], Decimal("135.00"))
        self.assertEqual(response.context["supplier_debt_total"], Decimal("280.00"))
        self.assertEqual(response.context["customer_advance_total"], Decimal("25.00"))
        self.assertEqual(response.context["supplier_advance_total"], Decimal("35.00"))
        self.assertEqual(response.context["bank_unmatched_in"], Decimal("30.00"))
        self.assertEqual(response.context["bank_unmatched_out"], Decimal("0.00"))
        self.assertEqual(response.context["payment_unmatched_in"], Decimal("0.00"))
        self.assertEqual(response.context["payment_unmatched_out"], Decimal("0.00"))
        self.assertEqual(response.context["active_matches"], 2)

    def test_controls_report_flags_voided_cash_payment_without_reversal(self):
        from decimal import Decimal
        from core.models import CashOperation, Customer, Sale, SettlementPayment, Shift
        customer = Customer.objects.create(company=self.company, name="Reversal customer", phone="+380501234568")
        sale = Sale.objects.create(company=self.company, cashier=self.user, customer=customer,
                                   total=Decimal("100.00"), status=Sale.DONE)
        shift = Shift.objects.create(company=self.company, cashier=self.user)
        operation = CashOperation.objects.create(company=self.company, shift=shift, user=self.user,
                                                 kind="in", amount=Decimal("20.00"))
        SettlementPayment.objects.create(company=self.company, sale=sale, amount=Decimal("20.00"),
                                         payment_method="cash", cash_operation=operation,
                                         created_by=self.user, is_void=True,
                                         void_reason="Помилковий платіж")
        response = self.client.get("/reports/finance/controls/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["reversal_gaps"]), 1)


    def test_controls_report_identifies_customer_refunds_due_and_supplier_credit(self):
        from decimal import Decimal
        from core.models import Customer, Purchase, Sale, SaleReturn, Supplier, SupplierCreditApplication
        customer = Customer.objects.create(company=self.company, name="Credit customer", phone="+380501234569")
        supplier = Supplier.objects.create(company=self.company, name="Credit supplier")
        sale = Sale.objects.create(company=self.company, cashier=self.user, customer=customer,
                                   total=Decimal("100.00"), cash_amount=Decimal("100.00"),
                                   status=Sale.DONE)
        SaleReturn.objects.create(company=self.company, sale=sale, user=self.user,
                                  total=Decimal("20.00"), refund_method="pending")
        source = Purchase.objects.create(company=self.company, supplier=supplier, user=self.user,
                                         total=Decimal("100.00"))
        target = Purchase.objects.create(company=self.company, supplier=supplier, user=self.user,
                                         total=Decimal("200.00"))
        SupplierCreditApplication.objects.create(company=self.company, source_purchase=source,
                                                 target_purchase=target, amount=Decimal("130.00"),
                                                 created_by=self.user)
        response = self.client.get("/reports/finance/controls/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["customer_credit_total"], Decimal("20.00"))
        self.assertEqual(response.context["supplier_credit_total"], Decimal("30.00"))


    def test_controls_report_period_cashflow_and_shift_closing(self):
        from decimal import Decimal
        from django.utils import timezone
        from core.models import BankAccount, BankTransaction, CashOperation, Shift
        shift = Shift.objects.create(
            company=self.company, cashier=self.user, opening_cash=Decimal("100.00"),
            drawer={"10000": 2}, closing_counted={"10000": 1},
            discrepancy=Decimal("-100.00"), closed_at=timezone.now(),
        )
        CashOperation.objects.create(company=self.company, shift=shift, user=self.user,
                                     kind="in", amount=Decimal("50.00"))
        CashOperation.objects.create(company=self.company, shift=shift, user=self.user,
                                     kind="out", amount=Decimal("-10.00"))
        CashOperation.objects.create(company=self.company, shift=shift, user=self.user,
                                     kind="adjust", amount=Decimal("5.00"))
        account = BankAccount.objects.create(company=self.company, bank_name="Period bank",
                                             iban="UA123456789012345678901234567")
        BankTransaction.objects.create(
            company=self.company, account=account, transaction_date=timezone.localdate(),
            direction=BankTransaction.INCOME, category="Надходження",
            amount=Decimal("70.00"), created_by=self.user,
        )
        BankTransaction.objects.create(
            company=self.company, account=account, transaction_date=timezone.localdate(),
            direction=BankTransaction.EXPENSE, category="Списання",
            amount=Decimal("20.00"), created_by=self.user,
        )
        response = self.client.get("/reports/finance/controls/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cash_period_in"], Decimal("50.00"))
        self.assertEqual(response.context["cash_period_out"], Decimal("10.00"))
        self.assertEqual(response.context["cash_adjustments"], Decimal("5.00"))
        self.assertEqual(response.context["period_cash_net"], Decimal("45.00"))
        self.assertEqual(response.context["bank_period_in"], Decimal("70.00"))
        self.assertEqual(response.context["bank_period_out"], Decimal("20.00"))
        self.assertEqual(response.context["period_bank_net"], Decimal("50.00"))
        self.assertEqual(response.context["period_net_movement"], Decimal("95.00"))
        self.assertEqual(response.context["closed_shift_count"], 1)
        self.assertEqual(response.context["shift_opening_total"], Decimal("100.00"))
        self.assertEqual(response.context["shift_expected_total"], Decimal("200.00"))
        self.assertEqual(response.context["shift_counted_total"], Decimal("100.00"))
        self.assertEqual(response.context["shift_discrepancy_total"], Decimal("-100.00"))


    def test_controls_report_csv_export_neutralizes_formula_injection(self):
        from decimal import Decimal
        from core.models import Customer, Sale, Supplier, Purchase
        customer = Customer.objects.create(company=self.company, name="=1+1", phone="+380501234570")
        supplier = Supplier.objects.create(company=self.company, name="@SUM(A1:A2)")
        Sale.objects.create(company=self.company, cashier=self.user, customer=customer,
                            total=Decimal("100.00"), status=Sale.DONE)
        Purchase.objects.create(company=self.company, supplier=supplier, user=self.user,
                                 total=Decimal("80.00"))
        response = self.client.get("/reports/finance/controls/?export=csv")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8-sig")
        self.assertIn("'=1+1", content)
        self.assertIn("'@SUM(A1:A2)", content)

    def test_controls_report_csv_export_contains_period_summary(self):
        response = self.client.get("/reports/finance/controls/?export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Борг клієнтів".encode("utf-8"), response.content)
        self.assertIn("Сумарний рух коштів".encode("utf-8"), response.content)
