import io
import json
import tempfile
from datetime import timedelta
from decimal import Decimal

import openpyxl
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (CrossRef, Customer, MarkupTier, Product, Sale, SaleItem, StockMovement, Supplier,
                         SupplierItem, norm_sku)

D = Decimal


def xlsx_bytes(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class TransitTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        self.mgr_user = U.objects.create_user("anna", password="x")
        self.mgr2 = U.objects.create_user("petro", password="x")
        self.adm, self.mgr, self.mgr2c = self.client, self.client_class(), self.client_class()
        self.adm.login(username="admin", password="x")
        self.mgr.login(username="anna", password="x")
        self.mgr2c.login(username="petro", password="x")
        for c in (self.mgr, self.mgr2c):
            c.post("/shift/", {"action": "open", "cash": "0"})
        self.sup = Supplier.objects.create(name="Busmarket")
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        self.sto = MarkupTier.objects.create(name="СТО", percent=15)

    # ---------- допоміжне
    def import_price(self, rows, sup=None, header=1):
        sup = sup or self.sup
        f = SimpleUploadedFile("price.xlsx", xlsx_bytes(rows))
        r = self.adm.post("/supplier-prices/map/", {"supplier": sup.pk, "file": f, "header_row": header})
        self.assertEqual(r.status_code, 200, r.content[:300])
        ctx = r.context
        data = {"supplier": sup.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": header}
        for fld in ctx["fields"]:
            if fld["sel"] is not None:
                data[f"col_{fld['key']}"] = fld["sel"]
        return self.adm.post("/supplier-prices/apply/", data, follow=True)

    def checkout(self, client, items, customer=None):
        r = client.post("/pos/checkout/", json.dumps({"items": items, "payment": "cash", "customer_id": customer}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return Sale.objects.get(pk=r.json()["id"])

    PRICE = [["Бренд", "Артикул", "Назва", "Ціна", "Залишок"],
             ["Bilstein", "19-152615", "Амортизатор задній Trafic II", "1000", "4"],
             ["SOLGY", "112021", "Кран печі Sprinter", "500,50", "є"],
             ["Gone", "X-1", "Зникне", "10", "0"]]

    # ---------- тести
    def test_norm_sku(self):
        self.assertEqual(norm_sku("19-152 615"), norm_sku("19152615"))
        self.assertEqual(norm_sku(" ab.12/3 "), "AB123")

    def test_price_import_and_reimport(self):
        r = self.import_price(self.PRICE)
        self.assertContains(r, "нових 3")
        a = SupplierItem.objects.get(sku="19-152615")
        self.assertEqual((a.brand, a.sku_norm, a.cost, a.qty_avail, a.available), ("BILSTEIN", "19152615", D("1000"), D("4"), True))
        self.assertEqual(SupplierItem.objects.get(sku="112021").cost, D("500.50"))       # кома як десятковий роздільник
        self.assertIsNone(SupplierItem.objects.get(sku="112021").qty_avail)              # «є» = наявність невідома
        self.assertFalse(SupplierItem.objects.get(sku="X-1").available)                  # залишок 0
        self.assertEqual(Product.objects.count(), 0)                                      # склад не засмічується
        r = self.import_price([self.PRICE[0], ["Bilstein", "19 152 615", "Амортизатор", "1100", "4"]])
        self.assertContains(r, "зникло з прайсу 1")
        self.assertEqual(SupplierItem.objects.filter(sku_norm="19152615").count(), 1)   # той самий артикул — не дубль
        self.assertEqual(SupplierItem.objects.get(sku_norm="19152615").cost, D("1100"))
        self.assertFalse(SupplierItem.objects.get(sku="112021").available)

    def test_lookup_analogs_and_stock_visibility(self):
        self.import_price(self.PRICE)
        own = Product.objects.create(name="Амортизатор SACHS Trafic", sku="SACHS-77", brand="SACHS", price=900,
                                     cost_price=700, current_stock=3)
        empty = Product.objects.create(name="Інший аналог без залишку", sku="NOSTOCK-1", price=1, current_stock=0)
        self.adm.post("/crossrefs/", {"act": "add", "a": "19-152615", "b": "sachs 77"})
        self.adm.post("/crossrefs/", {"act": "add", "a": "SACHS-77", "b": "NOSTOCK-1"})
        d = self.mgr.get("/pos/lookup/?q=19152615").json()
        self.assertEqual([x["s"] for x in d["analogs"]], ["SACHS-77"])                    # бачить, що вже лежить на складі
        self.assertEqual(d["analogs"][0]["st"], 3)
        self.assertEqual(d["suppliers"][0]["sku"], "19-152615")
        self.assertEqual(d["suppliers"][0]["kind"], "exact")
        self.assertNotIn("X-1", [x["sku"] for x in self.mgr.get("/pos/lookup/?q=X-1").json()["suppliers"]])  # немає в наявності — не показується
        # імпорт аналогів із файлу
        f = SimpleUploadedFile("c.xlsx", xlsx_bytes([["A", "B"], ["OE-1", "KOY-1"], ["KOY-1", "OE-1"]]))
        self.adm.post("/crossrefs/", {"act": "upload", "file": f, "has_header": "1"})
        self.assertTrue(CrossRef.objects.filter(a="KOY1", b="OE1").exists())

    def test_sale_from_supplier_skips_warehouse_and_margin(self):
        self.import_price(self.PRICE)
        si = SupplierItem.objects.get(sku="19-152615")
        sto_client = Customer.objects.create(name="СТО Вікторія", phone="1", markup_tier=self.sto)
        # ціна за націнкою клієнта (+15%) і ручна
        self.assertEqual(self.mgr.get("/pos/customer/?q=Вікторія").json()["results"][0]["pct"], 15.0)
        sale = self.checkout(self.mgr, [{"si": si.pk, "qty": 2, "price": 1150}], sto_client.pk)
        it = sale.items.get()
        self.assertEqual((it.source, it.fulfil_status, it.cost_price, it.price, it.original_price),
                         ("supplier", "ordered", D("1000.00"), D("1150.00"), D("1150.00")))
        self.assertEqual(it.markup_pct, D("15.00"))
        self.assertEqual(it.supplier, self.sup)
        p = it.product
        self.assertTrue(p.from_supplier)
        self.assertEqual(p.current_stock, 0)
        self.assertEqual(StockMovement.objects.filter(product=p).count(), 0)              # склад не зачіпається
        # технічна картка не засмічує каси, доки товару немає на складі
        self.assertNotIn(p.pk, [x["id"] for x in self.mgr.get("/pos/").context["init"]["products"]])
        # ручна ціна й дефолтна націнка 30% на сервері для роздрібного
        sale2 = self.checkout(self.mgr, [{"si": si.pk, "qty": 1, "price": None}])
        self.assertEqual(sale2.items.get().price, D("1300.00"))
        sale3 = self.checkout(self.mgr, [{"si": si.pk, "qty": 1, "price": "1234,5"}])
        self.assertEqual(sale3.items.get().price, D("1234.50"))
        self.assertEqual(Product.objects.filter(from_supplier=True).count(), 1)          # одна картка на артикул

    def test_stock_line_and_supplier_line_same_product_stay_separate(self):
        self.import_price(self.PRICE)
        si = SupplierItem.objects.get(sku="19-152615")
        from core import services
        p = services.ensure_product(si)
        p.current_stock = 2
        p.price = 1300
        p.save()
        sale = self.checkout(self.mgr, [{"id": p.pk, "qty": 2}, {"si": si.pk, "qty": 3, "price": 1300}])
        self.assertEqual(sale.items.count(), 2)
        p.refresh_from_db()
        self.assertEqual(p.current_stock, 0)                                              # зі складу списано тільки 2
        # на склад більше, ніж є, зі складу — не можна, а під замовлення — можна
        r = self.mgr.post("/pos/checkout/", json.dumps({"items": [{"id": p.pk, "qty": 1}]}), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_arrival_countdown_issue_stock_and_supplier_return(self):
        self.import_price(self.PRICE)
        si = SupplierItem.objects.get(sku="19-152615")
        c = Customer.objects.create(name="Іван", phone="2")
        sale = self.checkout(self.mgr, [{"si": si.pk, "qty": 2, "price": 1300}, {"si": si.pk, "qty": 1, "price": 1300}], c.pk)
        it = sale.items.get()                                                             # однакові позиції злилися
        self.assertEqual(it.quantity, 3)
        # чужий менеджер не може керувати чужою позицією
        self.assertEqual(self.mgr2c.post(f"/orders/{it.pk}/arrived/").status_code, 404)
        # 'прибув': відлік за налаштуванням 14 днів, але менеджер може задати свій
        self.mgr.post(f"/orders/{it.pk}/arrived/", {"days": ""})
        it.refresh_from_db()
        self.assertEqual((it.fulfil_status, it.pickup_deadline), ("arrived", timezone.localdate() + timedelta(days=14)))
        self.mgr.post(f"/orders/{it.pk}/arrived/", {"days": "7"})
        it.refresh_from_db()
        self.assertEqual(it.pickup_deadline, timezone.localdate() + timedelta(days=7))
        self.assertEqual(it.days_left, 7)
        self.assertEqual(self.mgr.post(f"/orders/{it.pk}/arrived/", {"days": "0"}, follow=True).status_code, 200)
        it.refresh_from_db()
        self.assertEqual(it.pickup_deadline, timezone.localdate() + timedelta(days=7))   # некоректне значення відхилено
        # сторінки
        self.assertContains(self.mgr.get("/orders/"), "залишилось 7 дн.")
        self.assertContains(self.adm.get("/orders/?st=arrived"), "Іван")
        self.assertEqual(self.adm.get("/").status_code, 200)
        # клієнт не забрав → на склад: склад отримав 3, позицію в чеку скасовано, повернення в обліку
        self.mgr.post(f"/orders/{it.pk}/stock/")
        it.refresh_from_db()
        p = it.product
        p.refresh_from_db()
        self.assertEqual((it.fulfil_status, it.returned_qty, p.current_stock), ("stocked", D("3"), D("3")))
        self.assertEqual(p.cost_price, D("1000.00"))
        mv = StockMovement.objects.get(product=p)
        self.assertEqual((mv.movement_type, mv.quantity_change), ("transit_stock", D("3")))
        self.assertIn(p.pk, [x["id"] for x in self.mgr.get("/pos/").context["init"]["products"]])  # тепер видно на касі
        self.mgr.post(f"/orders/{it.pk}/stock/")                                          # повторно — нічого не змінює
        p.refresh_from_db()
        self.assertEqual(p.current_stock, 3)
        # другий чек: видано клієнту транзитом (склад не чіпається)
        sale2 = self.checkout(self.mgr, [{"si": SupplierItem.objects.get(sku="112021").pk, "qty": 1, "price": 700}])
        it2 = sale2.items.get()
        self.mgr.post(f"/orders/{it2.pk}/issued/")
        it2.refresh_from_db()
        self.assertEqual(it2.fulfil_status, "issued")
        self.assertEqual(StockMovement.objects.filter(product=it2.product).count(), 0)
        # третій чек: повернення постачальнику — склад не змінюється, позиція скасована
        sale3 = self.checkout(self.mgr, [{"si": si.pk, "qty": 1, "price": 1300}])
        it3 = sale3.items.get()
        self.mgr.post(f"/orders/{it3.pk}/stock/")                                         # ще не прибув → відмова
        it3.refresh_from_db()
        self.assertEqual(it3.fulfil_status, "ordered")
        self.mgr.post(f"/orders/{it3.pk}/arrived/", {"days": "1"})
        self.mgr.post(f"/orders/{it3.pk}/supplier_return/")
        it3.refresh_from_db()
        p.refresh_from_db()
        self.assertEqual((it3.fulfil_status, p.current_stock), ("sup_return", D("3")))

    def test_return_of_pending_supplier_item_does_not_create_phantom_stock(self):
        self.import_price(self.PRICE)
        si = SupplierItem.objects.get(sku="19-152615")
        sale = self.checkout(self.mgr, [{"si": si.pk, "qty": 2, "price": 1300}])
        it = sale.items.get()
        self.adm.post(f"/sales/{sale.pk}/return/", {f"ret_{it.pk}": "2"})                # звичайне повернення чека
        it.refresh_from_db()
        self.assertEqual(it.fulfil_status, "cancelled")
        self.assertEqual(it.product.current_stock, 0)
        self.assertEqual(StockMovement.objects.filter(product=it.product).count(), 0)

    def test_manager_history_and_margin(self):
        self.import_price(self.PRICE)
        si = SupplierItem.objects.get(sku="19-152615")
        own = Product.objects.create(name="Свічка", sku="SV1", price=100, cost_price=60, current_stock=10)
        c = Customer.objects.create(name="Олег", phone="555")
        self.checkout(self.mgr, [{"si": si.pk, "qty": 2, "price": 1300}, {"id": own.pk, "qty": 5}], c.pk)
        self.checkout(self.mgr2c, [{"id": own.pk, "qty": 1}])
        r = self.mgr.get("/my-sales/")
        self.assertEqual(r.status_code, 200)
        # маржа: 2*(1300-1000) + 5*(100-60) = 800; виторг 2600+500 = 3100; Петро сюди не потрапляє
        self.assertEqual((r.context["rev"], r.context["cst"], r.context["mg"]), (D("3100"), D("2300"), D("800")))
        self.assertContains(r, "Олег")
        self.assertNotContains(r, "petro")
        r = self.adm.get("/my-sales/")
        self.assertEqual(r.context["rev"], D("3200"))
        self.assertEqual({s["sale__cashier__username"] for s in r.context["summary"]}, {"anna", "petro"})
        r = self.adm.get(f"/my-sales/?manager={self.mgr2.pk}")
        self.assertEqual(r.context["mg"], D("40"))
        r = self.adm.get("/my-sales/?src=supplier")
        self.assertEqual(r.context["mg"], D("600"))
        self.assertEqual(self.adm.get("/my-sales/?customer=555").context["rev"], D("3100"))
        csvt = self.adm.get("/my-sales/?csv=1").content.decode("utf-8-sig")
        self.assertIn("Олег;555", csvt)
        # часткове повернення зменшує маржу
        s = Sale.objects.filter(cashier=self.mgr_user).first()
        self.adm.post(f"/sales/{s.pk}/return/", {f"ret_{s.items.get(product=own).pk}": "5"})
        self.assertEqual(self.mgr.get("/my-sales/").context["mg"], D("600"))

    def test_pages_render_for_roles(self):
        for u in ["/orders/", "/my-sales/", "/pos/", "/pos/lookup/?q=abc"]:
            self.assertEqual(self.mgr.get(u).status_code, 200, u)
        for u in ["/supplier-prices/", "/crossrefs/", "/markups/", "/markups/new/", f"/markups/{self.sto.pk}/edit/", "/customers/new/", "/orders/", "/my-sales/", "/"]:
            self.assertEqual(self.adm.get(u).status_code, 200, u)
        for u in ["/supplier-prices/", "/crossrefs/", "/markups/"]:
            self.assertEqual(self.mgr.get(u).status_code, 403, u)                        # лише для адміністратора
        self.assertEqual(self.mgr.post("/supplier-prices/apply/", {}).status_code, 403)

    def test_bad_price_file(self):
        self.import_price(self.PRICE)
        r = self.adm.post("/supplier-prices/map/", {"supplier": self.sup.pk,
                          "file": SimpleUploadedFile("x.xls", b"garbage")}, follow=True)
        self.assertContains(r, "Старий формат .xls")
        r = self.adm.post("/supplier-prices/apply/", {"supplier": self.sup.pk, "token": "../../etc/passwd", "fname": "a.csv"}, follow=True)
        self.assertContains(r, "втрачено")
        self.assertEqual(SupplierItem.objects.filter(available=True).count(), 2)          # нічого не зламано
        csvf = SimpleUploadedFile("p.csv", "Артикул;Ціна;Назва\nA-1;12,5;Тест\n;5;без артикула\nB-2;abc;без ціни\n".encode("cp1251"))
        r = self.adm.post("/supplier-prices/map/", {"supplier": self.sup.pk, "file": csvf})
        ctx = r.context
        data = {"supplier": self.sup.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": 1}
        data.update({f"col_{f['key']}": f["sel"] for f in ctx["fields"] if f["sel"] is not None})
        r = self.adm.post("/supplier-prices/apply/", data, follow=True)
        self.assertContains(r, "нових 1")
        self.assertEqual(SupplierItem.objects.get(sku="A-1").cost, D("12.50"))
