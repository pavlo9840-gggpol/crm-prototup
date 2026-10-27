from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core import crosses, enrich
from core.models import CrossReject, CrossRef, PartOe, Product, Supplier, SupplierApi, SupplierItem

D = Decimal


class AutoCrossTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("cash", password="x")
        self.adm, self.cash = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.cash.login(username="cash", password="x")
        self.sup = Supplier.objects.create(name="S")

    def oe(self, brand, sku, oe):
        PartOe.objects.create(brand=brand, sku_norm="".join(c for c in sku if c.isalnum()).upper(), sku=sku,
                              oe_norm="".join(c for c in oe if c.isalnum()).upper(), oe_raw=oe, source="api")

    def item(self, brand, sku, name):
        return SupplierItem.objects.create(supplier=self.sup, brand=brand, sku=sku, sku_norm="".join(c for c in sku if c.isalnum()).upper(),
                                           name=name, cost=100)

    # ---- довіра зростає, коли джерела незалежно збігаються
    def test_score_grows_with_independent_sources(self):
        self.oe("A", "X1", "OE-1")
        self.oe("B", "ONLYOE", "OE-1")
        self.oe("B", "OEPLUSAPI", "OE-1")
        CrossRef.objects.create(a="OEPLUSAPI", b="X1", source="api")
        CrossRef.objects.create(a="FILE1", b="X1", source="import")
        res = {r["sku_norm"]: r for r in crosses.analogs("A", "X1")}
        self.assertEqual((res["ONLYOE"]["level"], res["ONLYOE"]["score"]), ("oe", 70))
        self.assertGreater(res["OEPLUSAPI"]["score"], res["ONLYOE"]["score"])            # OE + кросс від постачальника
        self.assertEqual(res["OEPLUSAPI"]["score"], 75)
        self.assertEqual((res["FILE1"]["level"], res["FILE1"]["score"]), ("cross", 45))
        self.assertEqual(len(res["OEPLUSAPI"]["evidence"]), 2)
        self.oe("A", "X1", "OE-2")
        self.oe("B", "ONLYOE", "OE-2")
        self.assertEqual({r["sku_norm"]: r for r in crosses.analogs("A", "X1")}["ONLYOE"]["score"], 80)   # два спільні OE

    # ---- виведення за назвою
    def test_name_inference_matches_type_vehicle_and_position_only(self):
        me = self.item("BILSTEIN", "19-152615", "Амортизатор (задній) Renault Trafic II 01- (B4)")
        self.item("SACHS", "315 849", "Амортизатор задній Renault Trafic II 2001-2014")           # той самий тип/авто/позиція
        self.item("MEYLE", "M-FRONT", "Амортизатор передній Renault Trafic II 01-")              # інша позиція
        self.item("KYB", "K-OTHERCAR", "Амортизатор задній Opel Vivaro 01-")                      # інше авто
        self.item("NGK", "N-1", "Свічка запалювання Renault Trafic II")                          # інший тип
        res = crosses.analogs("BILSTEIN", "19152615", with_names=True)
        self.assertEqual([(r["brand"], r["level"]) for r in res], [("SACHS", "name")])
        self.assertLessEqual(res[0]["score"], 40)
        self.assertIn("Збіг за назвою", res[0]["evidence"][0])
        self.assertEqual(crosses.analogs("BILSTEIN", "19152615"), [])                            # на касі (без назв) не підміняє точних
        # якщо є і OE, назва лише додає довіри
        self.oe("BILSTEIN", "19-152615", "OE-9")
        self.oe("SACHS", "315 849", "OE-9")
        top = crosses.analogs("BILSTEIN", "19152615", with_names=True)[0]
        self.assertEqual((top["level"], len(top["evidence"])), ("oe", 2))
        self.assertGreater(top["score"], 70)

    def test_name_profile_extracts_type_position_and_vehicle(self):
        p = crosses.name_profile("Амортизатор (задній) (L) Renault Trafic II 01- (B4)", "BILSTEIN")
        self.assertEqual(p["type"], "амортизатор")
        self.assertEqual(p["pos"], {"R", "L"})
        self.assertTrue({"renault", "trafic"} <= p["veh"])
        self.assertEqual(crosses.name_candidates("X", "Y", "Деталь"), {})                      # замало даних: нічого не вигадуємо

    # ---- навчання з рішень людей
    def test_confirm_and_reject_override_every_source(self):
        self.oe("A", "X1", "OE-1")
        self.oe("B", "P2", "OE-1")
        CrossRef.objects.create(a="P3", b="X1", source="import")
        self.assertEqual(crosses.analog_levels("X1"), {"P2": "oe", "P3": "cross"})
        crosses.reject_pair(None, "X1", "P2")
        self.assertEqual(crosses.analog_levels("X1"), {"P3": "cross"})                           # відхилена OE-пара прихована
        self.assertEqual([r["sku_norm"] for r in crosses.analogs("A", "X1")], ["P3"])
        self.assertEqual(crosses.analog_levels("P2"), {})
        crosses.confirm_pair(None, "X1", "P2")                                                    # передумали: підтвердили
        self.assertFalse(CrossReject.objects.exists())
        lv = crosses.analog_levels("X1")
        self.assertEqual(lv["P2"], "manual")
        r = {x["sku_norm"]: x for x in crosses.analogs("A", "X1")}
        self.assertEqual((r["P2"]["level"], r["P2"]["score"]), ("manual", 100))
        crosses.confirm_pair(None, "X1", "P3")
        self.assertEqual(CrossRef.objects.get(a="P3", b="X1").source, "manual")

    def test_card_actions_permissions(self):
        self.oe("A", "X1", "OE-1")
        self.oe("B", "P2", "OE-1")
        base = {"b": "A", "s": "X1", "os": "P2"}
        self.assertContains(self.cash.get("/part/?b=A&s=X1"), 'value="confirm"')
        self.assertNotContains(self.cash.get("/part/?b=A&s=X1"), 'value="reject"')               # відхиляти може лише адмін
        self.cash.post("/part/analog/", dict(base, act="reject"))
        self.assertFalse(CrossReject.objects.exists())
        self.cash.post("/part/analog/", dict(base, act="confirm"))
        self.assertEqual(CrossRef.objects.get().source, "manual")
        self.assertContains(self.adm.get("/part/?b=A&s=X1"), "Підтверджено вручну")
        self.adm.post("/part/analog/", dict(base, act="reject"))
        self.assertTrue(CrossReject.objects.exists())
        self.assertFalse(CrossRef.objects.filter(source="manual").exists())                       # підтвердження скасовано
        self.assertContains(self.adm.get("/part/?b=A&s=X1"), "Аналогів не знайдено")
        # відхилена пара зникає і з пошуку на касі
        self.item("B", "P2", "Деталь")
        self.assertEqual(self.cash.get("/pos/lookup/?q=X1").json()["suppliers"], [])

    # ---- безкоштовне джерело: кроси з картки постачальника
    def test_analogs_from_supplier_card_become_api_crosses(self):
        cfg = SupplierApi(supplier=self.sup, url="https://x", det_url="https://x/{id}")
        card = {"data": {"analogs": [{"article": "SACHS 315-849", "brand": "SACHS"}, "KYB 332 600", {"code": "19-152615"}]}}
        info = enrich.extract_info(card, cfg)
        self.assertEqual(info["analogs"], ["SACHS 315-849", "KYB 332 600", "19-152615"])
        enrich.save_info("BILSTEIN", "19-152615", info, "api")
        pairs = {(c.a, c.b, c.source) for c in CrossRef.objects.all()}
        self.assertEqual(pairs, {("19152615", "SACHS315849", "api"), ("19152615", "KYB332600", "api")})   # себе не додано
        enrich.save_info("BILSTEIN", "19-152615", info, "api")                                       # повторно без дублікатів
        self.assertEqual(CrossRef.objects.count(), 2)
        r = {x["sku_norm"]: x for x in crosses.analogs("BILSTEIN", "19152615")}
        self.assertEqual((r["SACHS315849"]["level"], r["SACHS315849"]["score"]), ("cross", 55))
        self.assertIn("Кросс із API постачальника", r["SACHS315849"]["evidence"])
