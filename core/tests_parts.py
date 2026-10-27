import json
import tempfile
from io import StringIO
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from core import apisync, crosses, enrich
from core.apisync import SyncError
from core.models import CrossRef, PartInfo, PartOe, Product, Supplier, SupplierApi, SupplierItem

D = Decimal

# Зразок прайсу BM Parts (формат із developer.bm.parts/tutorials/prices): два рядки заголовків, склади в колонках QTY:*
BM_CSV = (
    '"ІД","Артикул","Бренд","Назва","Ціна ГРН","Дніпро ДАГ","Київ"\n'
    '"UUID","Article","Brand","Name","Price","QTY:DAG:81EE005056AC66D611EA758E79EBACF9","QTY:KYV:81F4005056AC66D611EAD7173350C7AA"\n'
    '"80006E98C49AE02F4CB01012B3CA9C36","VM41023","VIBRA MASTER","Підшипник підвісний Land Rover Freelander","1206.96","1","2"\n'
    '"8001FBE703F98FB947F19E8AACB88834","02.53.071","TRUCKTEC AUTOMOTIVE","Ручка дверей MB Sprinter/VW LT 96-","277.44","1",""\n'
    '"800227FC8C8B4D304502B1C1D153453F","29-16 060 0008/HD","MEYLE","Тяга стабілізатора (L) Chevrolet Lacetti","417.24","",""\n'
).encode("utf-8")


class Net:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, body=None):
        self.calls.append((url, dict(headers), body))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, bytes) else json.dumps(r).encode()


class CrossEngineTest(TestCase):
    def setUp(self):
        self.sup = Supplier.objects.create(name="S")

    def oe(self, brand, sku, *oes, ob="VAG"):
        for o in oes:
            PartOe.objects.create(brand=brand, sku_norm="".join(c for c in sku if c.isalnum()).upper(), sku=sku,
                                  oe_norm="".join(c for c in o if c.isalnum()).upper(), oe_raw=o, oe_brand=ob)

    def test_shared_oe_gives_exact_analogs_with_evidence(self):
        self.oe("BILSTEIN", "19-152615", "1K0 411 105 AB", "1K0411105AC")
        self.oe("SACHS", "315 849", "1K0411105AB")                       # спільний OE (записаний інакше)
        self.oe("MEYLE", "M-1", "ZZZ999")                                # чужий OE, не аналог
        Product.objects.create(name="Амортизатор SACHS", sku="315 849", brand="SACHS", price=900, current_stock=3)
        SupplierItem.objects.create(supplier=self.sup, brand="SACHS", sku="315 849", sku_norm="315849", cost=700, name="Амортизатор SACHS")
        res = crosses.analogs("BILSTEIN", "19152615")
        self.assertEqual([(r["brand"], r["sku_norm"], r["level"]) for r in res], [("SACHS", "315849", "oe")])
        self.assertEqual(res[0]["shared"], ["1K0 411 105 AB"])         # так, як записано в самої деталі
        self.assertEqual((res[0]["stock"], res[0]["price"], res[0]["offer_cost"]), (3, D("900"), D("700")))
        # зворотний напрямок
        self.assertEqual([r["sku_norm"] for r in crosses.analogs("SACHS", "315849")], ["19152615"])
        self.assertEqual(crosses.analogs("MEYLE", "M1"), [])

    def test_levels_rank_oe_over_manual_over_cross_over_maybe(self):
        self.oe("A", "X1", "OE1")
        self.oe("B", "OEPART", "OE1")
        CrossRef.objects.create(a="X1", b="MAN1", source="manual")
        CrossRef.objects.create(a="X1", b="CRS1", source="import")
        CrossRef.objects.create(a="CRS1", b="FAR1", source="import")       # через ланцюжок
        CrossRef.objects.create(a="X1", b="OEPART", source="import")       # і крос, і OE: лишається найкращий
        lv = crosses.analog_levels("X1")
        self.assertEqual(lv, {"OEPART": "oe", "MAN1": "manual", "CRS1": "cross", "FAR1": "maybe"})

    def test_typing_the_oe_number_itself_finds_parts(self):
        self.oe("BILSTEIN", "19-152615", "1K0 411 105 AB")
        self.oe("SACHS", "315849", "1K0411105AB")
        self.assertEqual(crosses.analog_levels("1K0411105AB"), {"19152615": "oe", "315849": "oe"})


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class EnrichTest(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser("admin", "", "x")
        get_user_model().objects.create_user("cash", password="x")
        self.adm, self.cash = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.cash.login(username="cash", password="x")
        self.sup = Supplier.objects.create(name="Busmarket")
        self.cfg = SupplierApi.objects.create(
            supplier=self.sup, url="https://api.bm.parts/prices/list", fmt="csv", method="POST", auth_mode="header",
            auth_name="Authorization", api_key="TOKEN-XYZ", header_row=2, f_id="UUID", f_sku="Article", f_brand="Brand",
            f_name="Name", f_cost="Price", f_qty="QTY:*", body='{"currency": "C1", "warehouses": ["W1"]}',
            det_url="https://api.bm.parts/product/{id}?oe=full", det_image_base="https://cdn.example.com")

    CARD = {"product": {"uuid": "U1", "description": "<p>Амортизатор <b>газомасляний</b></p>",
                        "oe": [{"code": "1K0 411 105 AB", "brand": "vag"}, {"code": "1K0411105AC", "brand": "VAG"}, "WVA 1234"],
                        "attributes": [{"name": "Сторона", "value": "задня"}, {"name": "Тип", "value": "газовий"}],
                        "cars": [{"make": "Renault", "model": "Trafic II", "years": "2001-2014"}, "Opel Vivaro"],
                        "default_image": "photo/19152615.jpeg"}}

    # ---------------- прайс BM Parts: формат із документації
    def test_bm_price_csv_two_header_rows_multi_warehouse_post_and_raw_token(self):
        net = Net(BM_CSV)
        msg = apisync.sync_supplier(Supplier.objects.select_related("api").get(pk=self.sup.pk), None, fetch=net)
        self.assertIn("отримано 3", msg)
        url, headers, body = net.calls[0]
        self.assertEqual(url, "https://api.bm.parts/prices/list")
        self.assertEqual(headers["Authorization"], "TOKEN-XYZ")             # токен без «Bearer», як у документації
        self.assertEqual(headers["User-Agent"], "RetailShop/1.0")           # обов'язковий заголовок
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"currency": "C1", "warehouses": ["W1"]})
        a = SupplierItem.objects.get(sku="VM41023")
        self.assertEqual((a.brand, a.cost, a.qty_avail, a.ext_id), ("VIBRA MASTER", D("1206.96"), D("3"), "80006E98C49AE02F4CB01012B3CA9C36"))
        self.assertEqual(SupplierItem.objects.get(sku="02.53.071").qty_avail, D("1"))   # «Київ» порожньо = 0
        self.assertFalse(SupplierItem.objects.get(sku="29-16 060 0008/HD").available)    # на жодному складі немає

    def test_preset_fills_bm_settings_keeps_key_and_requires_real_uuids(self):
        self.cfg.delete()
        r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", {"act": "preset", "preset": "bmparts"}, follow=True)
        self.assertContains(r, "Застосовано шаблон")
        cfg = SupplierApi.objects.get(supplier=self.sup)
        self.assertEqual((cfg.url, cfg.method, cfg.header_row, cfg.f_qty, cfg.auth_name),
                         ("https://api.bm.parts/prices/list", "POST", 2, "QTY:*", "Authorization"))
        cfg.api_key = "TOKEN"
        cfg.save()
        with self.assertRaises(SyncError) as cm:                            # підказки в тілі не замінено
            apisync.sync_supplier(Supplier.objects.select_related("api").get(pk=self.sup.pk), None, fetch=Net(BM_CSV))
        self.assertIn("ВСТАВТЕ", str(cm.exception))
        self.assertEqual(self.cash.post(f"/suppliers/{self.sup.pk}/api/", {"act": "preset", "preset": "bmparts"}).status_code, 403)

    def test_form_rejects_invalid_json_body(self):
        data = {"url": self.cfg.url, "method": "POST", "body": "{нісенітниця", "fmt": "csv", "auth_mode": "header",
                "auth_name": "Authorization", "f_sku": "Article", "f_cost": "Price", "max_pages": 1, "header_row": 2, "act": "save"}
        self.assertContains(self.adm.post(f"/suppliers/{self.sup.pk}/api/", data), "коректним JSON")

    # ---------------- розбір картки
    def test_extract_info_autodetects_and_cleans(self):
        info = enrich.extract_info(self.CARD, self.cfg)
        self.assertEqual(info["description"], "Амортизатор газомасляний")           # HTML прибрано
        self.assertEqual([(c, n, b) for c, n, b in info["oe"]],
                         [("1K0 411 105 AB", "1K0411105AB", "VAG"), ("1K0411105AC", "1K0411105AC", "VAG"), ("WVA 1234", "WVA1234", "")])
        self.assertEqual(info["attrs"], "Сторона: задня\nТип: газовий")
        self.assertEqual(info["cars"], "Renault Trafic II 2001-2014\nOpel Vivaro")
        self.assertEqual(info["image_url"], "https://cdn.example.com/photo/19152615.jpeg")

    def test_extract_info_configured_paths_and_other_shapes(self):
        self.cfg.det_oe, self.cfg.det_desc = "data.refs", "data.about"
        obj = {"data": {"refs": {"BMW": ["11 22 33", "44 55 66"], "AUDI": "77 88, 99"}, "about": "Колодки"}}
        info = enrich.extract_info(obj, self.cfg)
        self.assertEqual(sorted((n, b) for _, n, b in info["oe"]), [("112233", "BMW"), ("445566", "BMW"), ("7788", "AUDI"), ("99", "AUDI")])
        self.assertEqual(info["description"], "Колодки")
        empty = enrich.extract_info({"x": 1}, self.cfg)
        self.assertEqual((empty["oe"], empty["description"], empty["image_url"]), ([], "", ""))

    def test_save_info_protects_manual_edits_and_manual_oe(self):
        enrich.save_info("BILSTEIN", "19-152615", {"oe": [("A1", "A1", "VAG")], "description": "з API"}, "api")
        PartOe.objects.create(brand="BILSTEIN", sku_norm="19152615", oe_norm="MAN", oe_raw="MAN", source="manual")
        pi = PartInfo.objects.get(brand="BILSTEIN")
        pi.description, pi.source = "Мій опис", "manual"
        pi.save()
        enrich.save_info("BILSTEIN", "19-152615", {"oe": [("A2", "A2", "")], "description": "новий з API", "cars": "Opel"}, "api")
        pi.refresh_from_db()
        self.assertEqual((pi.description, pi.cars), ("Мій опис", "Opel"))       # ручний опис не перезаписано, порожнє поле заповнено
        self.assertEqual(sorted(PartOe.objects.filter(brand="BILSTEIN").values_list("oe_norm", flat=True)), ["A2", "MAN"])

    # ---------------- підтягування з картки
    def seed_items(self, n=3):
        for i in range(n):
            SupplierItem.objects.create(supplier=self.sup, brand="B", sku=f"S{i}", sku_norm=f"S{i}", cost=10, ext_id=f"ID{i}")

    def test_enrich_item_batch_skips_done_and_handles_errors(self):
        self.seed_items(4)
        net = Net(self.CARD)
        sup = Supplier.objects.select_related("api").get(pk=self.sup.pk)
        done, errors = enrich.enrich_batch(sup, limit=2, fetch=net)
        self.assertEqual((done, errors, len(net.calls)), (2, 0, 2))
        self.assertEqual(net.calls[0][0], "https://api.bm.parts/product/ID0?oe=full")
        self.assertEqual(net.calls[0][1]["Authorization"], "TOKEN-XYZ")
        done, _ = enrich.enrich_batch(sup, limit=10, fetch=Net(self.CARD))       # лишилось тільки 2 необроблених
        self.assertEqual(done, 2)
        self.assertEqual(enrich.enrich_batch(sup, limit=10, fetch=Net(self.CARD)), (0, 0))
        self.assertEqual(PartOe.objects.filter(brand="B", sku_norm="S0").count(), 3)

    def test_enrich_batch_stops_on_rate_limit_and_marks_404(self):
        self.seed_items(3)
        sup = Supplier.objects.select_related("api").get(pk=self.sup.pk)
        with self.assertRaises(SyncError) as cm:
            enrich.enrich_batch(sup, limit=5, fetch=Net(SyncError("Постачальник відповів HTTP 429: перевищено ліміт")))
        self.assertIn("обмежив частоту", str(cm.exception))
        enrich.enrich_batch(sup, limit=5, fetch=Net(SyncError("Постачальник відповів HTTP 404: адресу не знайдено")))
        self.assertEqual(PartInfo.objects.count(), 3)                              # позначено, щоб не питати вічно
        self.assertEqual(enrich.enrich_batch(sup, limit=5, fetch=Net(self.CARD)), (0, 0))

    def test_enrich_needs_id_and_url(self):
        si = SupplierItem.objects.create(supplier=self.sup, brand="B", sku="X", sku_norm="X", cost=1)
        with self.assertRaises(SyncError) as cm:
            enrich.enrich_item(SupplierItem.objects.select_related("supplier__api").get(pk=si.pk), fetch=Net(self.CARD))
        self.assertIn("немає ID", str(cm.exception))
        self.cfg.det_url = "https://x/no-placeholder"
        self.cfg.save()
        si.ext_id = "1"
        si.save()
        with self.assertRaises(SyncError):
            enrich.enrich_item(SupplierItem.objects.select_related("supplier__api").get(pk=si.pk), fetch=Net(self.CARD))

    def test_probe_and_enrich_buttons_and_command(self):
        self.seed_items(2)
        with mock.patch("core.apisync.fetch_bytes", Net(self.CARD)):
            r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", {"act": "probe"})
            self.assertContains(r, "Структура картки")
            self.assertContains(r, "product.oe: список (3)")
            self.assertNotContains(r, "TOKEN-XYZ")
            r = self.adm.post(f"/suppliers/{self.sup.pk}/api/", {"act": "enrich"}, follow=True)
            self.assertContains(r, "Підтягнуто дані про деталі: 2")
            SupplierItem.objects.create(supplier=self.sup, brand="B", sku="Z9", sku_norm="Z9", cost=1, ext_id="IDZ")
            call_command("enrich_parts", stdout=StringIO())
        self.assertTrue(PartInfo.objects.filter(brand="B", sku_norm="Z9").exists())
        self.assertEqual(self.cash.post(f"/suppliers/{self.sup.pk}/api/", {"act": "enrich"}).status_code, 403)

    # ---------------- картка запчастини, редагування, імпорт, каса
    def build_catalog(self):
        SupplierItem.objects.create(supplier=self.sup, brand="BILSTEIN", sku="19-152615", sku_norm="19152615", cost=1000,
                                    name="Амортизатор задній Trafic", ext_id="U1")
        SupplierItem.objects.create(supplier=self.sup, brand="SACHS", sku="315 849", sku_norm="315849", cost=800, name="Амортизатор")
        Product.objects.create(name="Амортизатор SACHS", sku="315 849", brand="SACHS", price=1100, current_stock=2)
        PartOe.objects.create(brand="BILSTEIN", sku_norm="19152615", oe_norm="1K0411105AB", oe_raw="1K0 411 105 AB", oe_brand="VAG", source="api")
        PartOe.objects.create(brand="SACHS", sku_norm="315849", oe_norm="1K0411105AB", oe_raw="1K0411105AB", source="import")

    def test_part_card_shows_analogs_stock_offers_and_permissions(self):
        self.build_catalog()
        r = self.cash.get("/part/?b=BILSTEIN&s=19-152615")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Точний аналог (спільний OE)")
        self.assertContains(r, "1K0 411 105 AB")
        self.assertContains(r, "Оновити OE-коди й опис з API")
        self.assertNotContains(r, "Редагувати дані про деталь")                  # лише для адміністратора
        self.assertContains(self.adm.get("/part/?b=BILSTEIN&s=19152615"), "Редагувати дані про деталь")
        self.assertEqual(self.cash.get("/part/?b=NOPE&s=000").status_code, 302)  # невідома деталь
        # ручне редагування (адмін) і заборона для менеджера
        data = {"b": "BILSTEIN", "s": "19152615", "sku": "19-152615", "description": "Ручний опис", "attrs": "Сторона: задня",
                "cars": "Renault Trafic", "image_url": "", "oe": "ZZ 111;RENAULT"}
        self.assertEqual(self.cash.post("/part/edit/", data).status_code, 403)
        self.adm.post("/part/edit/", data)
        r = self.adm.get("/part/?b=BILSTEIN&s=19152615")
        self.assertContains(r, "Ручний опис")
        self.assertContains(r, "Сторона")
        self.assertContains(r, "ZZ 111")
        # повторне збереження замінює свої ручні OE і не чіпає чужі (api)
        self.adm.post("/part/edit/", dict(data, oe=""))
        self.assertEqual(sorted(PartOe.objects.filter(brand="BILSTEIN").values_list("oe_norm", flat=True)), ["1K0411105AB"])

    def test_part_refresh_button(self):
        self.build_catalog()
        with mock.patch("core.apisync.fetch_bytes", Net(self.CARD)):
            r = self.cash.post("/part/refresh/", {"b": "BILSTEIN", "s": "19152615"}, follow=True)
        self.assertContains(r, "Дані оновлено з API")
        self.assertTrue(PartOe.objects.filter(brand="BILSTEIN", oe_norm="WVA1234").exists())
        r = self.cash.post("/part/refresh/", {"b": "SACHS", "s": "315849"}, follow=True)      # у цієї позиції немає ID
        self.assertContains(r, "Немає постачальника з налаштованим API")

    def test_import_oe_and_descriptions_from_files(self):
        from core.tests_transit import xlsx_bytes
        f = SimpleUploadedFile("oe.xlsx", xlsx_bytes([["Бренд", "Артикул", "OE", "Бренд OE"], ["Meyle", "29-16 060 0008", "4D0 407 693", "AUDI"],
                                                      ["meyle", "29-16 060 0008", "4D0407693", "AUDI"], ["", "X", "Y", ""]]))
        r = self.adm.post("/parts-data/", {"kind": "oe", "file": f, "has_header": "1"}, follow=True)
        self.assertContains(r, "деталь ↔ OE»: 1")
        o = PartOe.objects.get()
        self.assertEqual((o.brand, o.sku_norm, o.oe_norm, o.oe_brand, o.source), ("MEYLE", "29160600008", "4D0407693", "AUDI", "import"))
        f = SimpleUploadedFile("d.csv", "Бренд;Артикул;Опис;Характеристики;Застосовність\nMeyle;29-16 060 0008;Тяга стабілізатора;Довжина: 120 мм|Сторона: ліва;Audi A4|Audi A6\n".encode("utf-8"))
        self.adm.post("/parts-data/", {"kind": "desc", "file": f, "has_header": "1"})
        pi = PartInfo.objects.get(brand="MEYLE")
        self.assertEqual((pi.description, pi.attrs, pi.cars), ("Тяга стабілізатора", "Довжина: 120 мм\nСторона: ліва", "Audi A4\nAudi A6"))
        self.assertEqual(self.cash.get("/parts-data/").status_code, 403)

    def test_pos_lookup_by_oe_and_levels(self):
        self.build_catalog()
        d = self.cash.get("/pos/lookup/?q=1K0411105AB").json()                       # менеджер ввів сам OE-номер
        self.assertEqual({(s["brand"], s["lv"]) for s in d["suppliers"]}, {("BILSTEIN", "oe"), ("SACHS", "oe")})
        self.assertEqual([(a["s"], a["lv"]) for a in d["analogs"]], [("315 849", "oe")])
        d = self.cash.get("/pos/lookup/?q=19152615").json()
        self.assertEqual(d["suppliers"][0]["kind"], "exact")
        sachs = [s for s in d["suppliers"] if s["brand"] == "SACHS"][0]
        self.assertEqual((sachs["kind"], sachs["lv"], sachs["stock"]), ("analog", "oe", 0.0))
        self.assertEqual([(a["s"], a["lv"]) for a in d["analogs"]], [("315 849", "oe")])
