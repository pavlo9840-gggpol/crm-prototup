import json
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from core import apisync
from core.models import Product, Supplier, SupplierApi, SupplierItem

D = Decimal


class BrandFormTest(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser("admin", "", "x")
        get_user_model().objects.create_user("cash", password="x")
        self.adm = self.client
        self.adm.login(username="admin", password="x")

    FORM = {"brand": "bilstein", "sku": "19-152615", "name": "Амортизатор задній", "unit": "шт", "cost_price": "1000",
            "price": "1300", "min_stock": "0", "initial_stock": "2", "is_active": "on"}

    def test_form_shows_brand_first_with_suggestions(self):
        Product.objects.create(name="X", sku="1", brand="SACHS")
        s = Supplier.objects.create(name="S")
        SupplierItem.objects.create(supplier=s, brand="MEYLE", sku="5", sku_norm="5", cost=1)
        r = self.adm.get("/products/new/")
        html = r.content.decode()
        self.assertLess(html.index('name="brand"'), html.index('name="sku"'))
        self.assertLess(html.index('name="sku"'), html.index('name="name"'))
        self.assertIn('list="brand-list"', html)
        self.assertIn('<option value="SACHS">', html)
        self.assertIn('<option value="MEYLE">', html)                   # бренди з прайсів теж підказуються
        self.assertIn("Що це за деталь", html)

    def test_brand_required_normalized_and_duplicates(self):
        r = self.adm.post("/products/new/", dict(self.FORM, brand=""))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "обов&#x27;язкове")                        # «Це поле обов'язкове»
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(self.adm.post("/products/new/", self.FORM).status_code, 302)
        p = Product.objects.get()
        self.assertEqual((p.brand, p.sku, p.sku_norm, p.current_stock), ("BILSTEIN", "19-152615", "19152615", 2))
        # той самий товар, інакше записаний артикул: відхиляється
        r = self.adm.post("/products/new/", dict(self.FORM, brand="Bilstein", sku="19 152615"))
        self.assertContains(r, "Такий товар уже є в базі")
        self.assertEqual(Product.objects.count(), 1)
        # той самий артикул в ІНШОГО бренду: це інший товар, дозволено
        self.assertEqual(self.adm.post("/products/new/", dict(self.FORM, brand="SACHS", name="Інший")).status_code, 302)
        self.assertEqual(Product.objects.filter(sku="19-152615").count(), 2)
        # редагування не конфліктує саме з собою
        self.assertEqual(self.adm.post(f"/products/{p.pk}/edit/", dict(self.FORM, name="Нова назва")).status_code, 302)

    def test_list_search_by_brand_and_normalized_article(self):
        self.adm.post("/products/new/", self.FORM)
        for q in ("bilstein", "19152615", "19-152 615", "152615"):
            self.assertContains(self.adm.get("/products/", {"q": q}), "Амортизатор задній", msg_prefix=q)
        self.assertNotContains(self.adm.get("/products/", {"q": "mann"}), "Амортизатор задній")
        self.assertContains(self.adm.get("/products/"), "<th>Бренд<th>Артикул")

    def test_csv_identity_is_brand_plus_article(self):
        Product.objects.create(name="A", sku="112021", brand="SOLGY", price=10)
        Product.objects.create(name="B", sku="112021", brand="SACHS", price=20)     # однаковий артикул, різні бренди
        csv_ = self.adm.get("/products/export/").content.decode("utf-8-sig")
        self.assertIn("Бренд", csv_.splitlines()[0])
        self.assertIn("SOLGY", csv_)
        up = lambda t: {"file": SimpleUploadedFile("p.csv", t.encode("utf-8-sig"))}
        # без бренду артикул неоднозначний
        r = self.adm.post("/products/import/", up("Код;Ціна продажу\n112021;99\n"))
        self.assertContains(r, "є в кількох брендів")
        # з брендом оновлюється саме потрібний
        self.adm.post("/products/import/", up("Код;Бренд;Назва;Ціна продажу\n112021;sachs;;99\n112021;NEWBRAND;Новий;5\n"))
        r = self.adm.post("/products/import/apply/", follow=True)
        self.assertContains(r, "створено 1, оновлено 1")
        self.assertEqual(Product.objects.get(brand="SACHS").price, D("99"))
        self.assertEqual(Product.objects.get(brand="SOLGY").price, D("10"))
        # дубль пари бренд+артикул у файлі
        r = self.adm.post("/products/import/", up("Код;Бренд;Назва\nZ-1;K;a\nZ 1;k;b\n"))
        self.assertContains(r, "повторюється у файлі")


# ====================================================================== API постачальника
class FakeNet:
    """Підміна мережі: віддає задані відповіді і запамʼятовує запити."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, bytes) else json.dumps(r).encode()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class SupplierApiTest(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser("admin", "", "x")
        get_user_model().objects.create_user("cash", password="x")
        self.adm, self.cash = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.cash.login(username="cash", password="x")
        self.sup = Supplier.objects.create(name="Busmarket")
        self.cfg = SupplierApi.objects.create(supplier=self.sup, url="https://api.example.com/v1/products", fmt="json",
                                              auth_mode="bearer", api_key="SECRET-KEY-123", items_path="data.items",
                                              f_sku="article", f_brand="brand.name", f_name="title", f_cost="price", f_qty="stock")

    def sup_(self):
        return Supplier.objects.select_related("api").get(pk=self.sup.pk)

    PAGE1 = {"data": {"items": [
        {"article": "19-152615", "brand": {"name": "Bilstein"}, "title": "Амортизатор", "price": 1000.5, "stock": 4},
        {"article": "112021", "brand": {"name": "Solgy"}, "title": "Кран печі", "price": "500,00", "stock": ">10"},
        {"article": "BAD", "brand": {"name": "X"}, "title": "Без ціни", "price": None, "stock": 1},
        {"article": "NOSTOCK", "brand": {"name": "Y"}, "title": "Немає", "price": 5, "stock": 0}]}}
    PAGE2 = {"data": {"items": [{"article": "777", "brand": {"name": "Meyle"}, "title": "Колодки", "price": 90, "stock": 2}]}}
    EMPTY = {"data": {"items": []}}

    def test_json_sync_pagination_auth_and_mapping(self):
        self.cfg.page_param = "page"
        self.cfg.save()
        net = FakeNet(self.PAGE1, self.PAGE2, self.EMPTY)
        msg = apisync.sync_supplier(self.sup_(), None, fetch=net)
        self.assertIn("отримано 4", msg)
        self.assertEqual(len(net.calls), 3)                              # сторінки 1, 2 і порожня
        self.assertEqual(net.calls[0][0], "https://api.example.com/v1/products?page=1")
        self.assertEqual(net.calls[1][0], "https://api.example.com/v1/products?page=2")
        self.assertEqual(net.calls[0][1]["Authorization"], "Bearer SECRET-KEY-123")
        a = SupplierItem.objects.get(sku="19-152615")
        self.assertEqual((a.brand, a.cost, a.qty_avail, a.available), ("BILSTEIN", D("1000.50"), D("4"), True))
        self.assertEqual(SupplierItem.objects.get(sku="112021").cost, D("500.00"))
        self.assertEqual(SupplierItem.objects.get(sku="112021").qty_avail, D("10"))     # «>10» → 10
        self.assertFalse(SupplierItem.objects.get(sku="NOSTOCK").available)
        self.assertFalse(SupplierItem.objects.filter(sku="BAD").exists())             # без ціни не імпортується
        self.assertTrue(SupplierItem.objects.filter(sku="777", brand="MEYLE").exists())
        self.cfg.refresh_from_db()
        self.assertIn("отримано 4", self.cfg.last_result)
        self.assertIsNotNone(self.cfg.last_sync_at)

    def test_api_ignoring_page_param_does_not_loop_forever(self):
        self.cfg.page_param, self.cfg.max_pages = "page", 50
        self.cfg.save()
        net = FakeNet(self.PAGE2)                                        # завжди одна й та сама відповідь
        apisync.sync_supplier(self.sup_(), None, fetch=net)
        self.assertEqual(len(net.calls), 2)
        self.assertEqual(SupplierItem.objects.count(), 1)

    def test_auth_modes(self):
        for mode, name, check in [("header", "X-Token", lambda u, h: h["X-Token"] == "SECRET-KEY-123"),
                                  ("header", "", lambda u, h: h["X-API-Key"] == "SECRET-KEY-123"),
                                  ("query", "apikey", lambda u, h: "apikey=SECRET-KEY-123" in u and "Authorization" not in h),
                                  ("none", "", lambda u, h: "SECRET" not in u and "Authorization" not in h)]:
            self.cfg.auth_mode, self.cfg.auth_name = mode, name
            self.cfg.save()
            net = FakeNet(self.PAGE2)
            apisync.sync_supplier(self.sup_(), None, fetch=net)
            self.assertTrue(check(*net.calls[0]), (mode, name, net.calls[0]))

    def test_xml_and_table_formats(self):
        self.cfg.fmt, self.cfg.items_path, self.cfg.auth_mode = "xml", "offer", "none"
        self.cfg.f_sku, self.cfg.f_brand, self.cfg.f_name, self.cfg.f_cost, self.cfg.f_qty = "vendorCode", "vendor", "name", "price", "@available"
        self.cfg.save()
        xml = ('<?xml version="1.0"?><!DOCTYPE yml_catalog SYSTEM "shops.dtd"><yml_catalog><shop><offers>'
               '<offer available="12"><vendorCode>AB-1</vendorCode><vendor>Febi</vendor><name>Фільтр</name><price>150.5</price></offer>'
               '<offer available="0"><vendorCode>AB-2</vendorCode><vendor>Febi</vendor><name>Свічка</name><price>20</price></offer>'
               '</offers></shop></yml_catalog>').encode()
        apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(xml))
        self.assertEqual(SupplierItem.objects.get(sku="AB-1").qty_avail, D("12"))
        self.assertFalse(SupplierItem.objects.get(sku="AB-2").available)
        # XML-бомба з ENTITY відхиляється
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><r><offer/></r>'
        with self.assertRaises(apisync.SyncError):
            apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(bomb))
        # CSV за посиланням, колонки за назвою і за літерою
        self.cfg.fmt, self.cfg.f_sku, self.cfg.f_brand, self.cfg.f_name, self.cfg.f_cost, self.cfg.f_qty = "csv", "Артикул", "Бренд", "Назва", "d", "Залишок"
        self.cfg.save()
        csv_ = "Артикул;Бренд;Назва;Ціна;Залишок\nQ-1;Mann;Фільтр;77,5;3\n".encode("utf-8")
        apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(csv_))
        self.assertEqual(SupplierItem.objects.get(sku="Q-1").cost, D("77.50"))          # «d» = четверта колонка (Ціна)

    # ---------------- запобіжники
    def test_empty_or_suspicious_response_never_wipes_catalog(self):
        for i in range(60):
            SupplierItem.objects.create(supplier=self.sup, brand="B", sku=f"S{i}", sku_norm=f"S{i}", cost=10, available=True)
        with self.assertRaises(apisync.SyncError) as cm:
            apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(self.EMPTY))
        self.assertIn("Каталог не змінено", str(cm.exception))
        self.assertEqual(SupplierItem.objects.filter(available=True).count(), 60)
        with self.assertRaises(apisync.SyncError) as cm:                  # 1 позиція замість 60
            apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(self.PAGE2))
        self.assertIn("підозріло мало", str(cm.exception))
        self.assertEqual(SupplierItem.objects.filter(available=True).count(), 60)
        self.cfg.refresh_from_db()
        self.assertIn("помилка", self.cfg.last_result)
        apisync.sync_supplier(self.sup_(), None, force=True, fetch=FakeNet(self.PAGE2))     # явне підтвердження
        self.assertEqual(SupplierItem.objects.filter(available=True).count(), 1)

    def test_key_never_leaks_into_errors(self):
        self.cfg.auth_mode, self.cfg.auth_name = "query", "key"
        self.cfg.save()
        err = apisync.SyncError("збій https://x/?key=SECRET-KEY-123")
        with self.assertRaises(apisync.SyncError) as cm:
            apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(err))
        self.assertNotIn("SECRET-KEY-123", str(cm.exception))
        self.cfg.refresh_from_db()
        self.assertNotIn("SECRET-KEY-123", self.cfg.last_result)
        for bad in (b"<html>login</html>", b""):
            with self.assertRaises(apisync.SyncError):
                apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(bad))
        self.cfg.items_path = "nope.here"
        self.cfg.save()
        with self.assertRaises(apisync.SyncError) as cm:
            apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(self.PAGE1))
        self.assertIn("немає списку товарів", str(cm.exception))

    def test_env_key_and_missing_key(self):
        import os
        self.cfg.api_key = "env:RETAIL_TEST_KEY"
        self.cfg.save()
        os.environ["RETAIL_TEST_KEY"] = "FROM-ENV"
        net = FakeNet(self.PAGE2)
        apisync.sync_supplier(self.sup_(), None, fetch=net)
        self.assertEqual(net.calls[0][1]["Authorization"], "Bearer FROM-ENV")
        del os.environ["RETAIL_TEST_KEY"]
        with self.assertRaises(apisync.SyncError) as cm:
            apisync.sync_supplier(self.sup_(), None, fetch=FakeNet(self.PAGE2))
        self.assertIn("ключ не заданий", str(cm.exception))

    def test_ssrf_guard(self):
        for url in ("http://127.0.0.1/x", "http://localhost:8000/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data",
                    "file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"):
            with self.assertRaises(apisync.SyncError, msg=url):
                apisync.check_url(url)

    # ---------------- сторінки
    def test_pages_hide_key_and_require_staff(self):
        r = self.adm.get(f"/suppliers/{self.sup.pk}/api/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "SECRET-KEY-123")
        self.assertContains(r, "Ключ збережено")
        self.assertEqual(self.cash.get(f"/suppliers/{self.sup.pk}/api/").status_code, 403)
        self.assertEqual(self.cash.post(f"/suppliers/{self.sup.pk}/api/", {"act": "sync"}).status_code, 403)
        # порожнє поле ключа не стирає збережений; новий замінює; «видалити» очищає
        data = {"url": self.cfg.url, "fmt": "json", "auth_mode": "bearer", "items_path": "data.items", "f_sku": "article",
                "f_cost": "price", "max_pages": 500, "act": "save"}
        self.adm.post(f"/suppliers/{self.sup.pk}/api/", data)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.api_key, "SECRET-KEY-123")
        self.adm.post(f"/suppliers/{self.sup.pk}/api/", dict(data, api_key_new="NEW-KEY"))
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.api_key, "NEW-KEY")
        r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", dict(data, clear_key="on"))
        self.assertContains(r, "Вкажіть API-ключ")                         # ключ потрібен, якщо режим не «Без ключа»
        r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", dict(data, url="ftp://x"))
        self.assertContains(r, "http://")
        self.assertEqual(self.adm.get("/supplier-prices/").status_code, 200)
        self.assertContains(self.adm.get("/supplier-prices/"), "Оновити через API")

    def test_test_button_does_not_write_and_sync_button_does(self):
        from unittest import mock
        with mock.patch("core.apisync.fetch_bytes", FakeNet(self.PAGE1)):
            r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", {"act": "test"})
            self.assertContains(r, "Приклад прочитаних позицій")
            self.assertContains(r, "19-152615")
            self.assertEqual(SupplierItem.objects.count(), 0)
            r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", {"act": "sync", "next": "supplier_prices"}, follow=True)
            self.assertContains(r, "отримано 3")
            self.assertEqual(SupplierItem.objects.count(), 3)
        with mock.patch("core.apisync.fetch_bytes", FakeNet(apisync.SyncError("Постачальник відповів HTTP 401: ключ відхилено"))):
            r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", {"act": "sync", "next": "supplier_prices"}, follow=True)
            self.assertContains(r, "HTTP 401")
            self.assertEqual(SupplierItem.objects.filter(available=True).count(), 2)         # каталог цілий

    def test_management_command(self):
        from unittest import mock
        out = StringIO()
        with mock.patch("core.apisync.fetch_bytes", FakeNet(self.PAGE1)):
            call_command("sync_prices", stdout=out)
        self.assertIn("Busmarket: ", out.getvalue())
        self.assertEqual(SupplierItem.objects.count(), 3)
        with mock.patch("core.apisync.fetch_bytes", FakeNet(apisync.SyncError("boom"))):
            with self.assertRaises(SystemExit):
                call_command("sync_prices", stdout=StringIO(), stderr=StringIO())

    # ---------------- справжній HTTP-сервер (без підміни мережі)
    @override_settings(API_ALLOW_PRIVATE_HOSTS=True)
    def test_real_http_roundtrip(self):
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"], seen["path"] = self.headers.get("Authorization"), self.path
                body = json.dumps(SupplierApiTest.PAGE2).encode()
                self.send_response(200 if seen["auth"] == "Bearer SECRET-KEY-123" else 401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body if seen["auth"] == "Bearer SECRET-KEY-123" else b"{}")

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.cfg.url = f"http://127.0.0.1:{srv.server_port}/api?lang=uk"
            self.cfg.save()
            apisync.sync_supplier(self.sup_(), None)
            self.assertEqual(seen["auth"], "Bearer SECRET-KEY-123")
            self.assertEqual(seen["path"], "/api?lang=uk")
            self.assertEqual(SupplierItem.objects.get().sku, "777")
            self.cfg.api_key = "WRONG"
            self.cfg.save()
            with self.assertRaises(apisync.SyncError) as cm:
                apisync.sync_supplier(self.sup_(), None)
            self.assertIn("HTTP 401", str(cm.exception))
            self.assertNotIn("WRONG", str(cm.exception))
        finally:
            srv.shutdown()
