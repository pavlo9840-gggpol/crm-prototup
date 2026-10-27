import json
import re
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from core import pricing, services
from core.models import (Category, Customer, MarkupTier, PriceHistory, PriceRule, Product, Supplier, SupplierItem, SupplierPriceChange)
from core.tests_transit import xlsx_bytes

D = Decimal


def rule(**kw):
    kw.setdefault("name", "r")
    return PriceRule.objects.create(**kw)


class PricingEngineTest(TestCase):
    def test_most_specific_rule_wins_with_category_inheritance_and_priority(self):
        parent = Category.objects.create(name="Підвіска")
        child = Category.objects.create(name="Амортизатори", parent=parent)
        other = Category.objects.create(name="Гальма")
        general = rule(name="загальне", markup_pct=30)
        by_brand = rule(name="бренд", brand="BILSTEIN", markup_pct=40)
        by_cat = rule(name="категорія", category=parent, markup_pct=35)
        both = rule(name="бренд+кат", brand="BILSTEIN", category=parent, markup_pct=50)
        rule(name="вимкнене", brand="SACHS", markup_pct=99, is_active=False)
        rs = pricing.RuleSet()
        self.assertEqual(rs.for_("bilstein", child.pk), both)                 # підкатегорія успадковує правило батьківської
        self.assertEqual(rs.for_("BILSTEIN", other.pk), by_brand)
        self.assertEqual(rs.for_("SACHS", child.pk), by_cat)                  # вимкнене правило не діє
        self.assertEqual(rs.for_("SACHS", other.pk), general)
        self.assertEqual(rs.for_("", None), general)
        near = rule(name="ближча категорія", category=child, markup_pct=36)
        self.assertEqual(pricing.RuleSet().for_("SACHS", child.pk), near)       # конкретніша (ближча) категорія виграє
        a, b = rule(name="a", brand="MEYLE", priority=50), rule(name="b", brand="MEYLE", priority=10)
        self.assertEqual(pricing.RuleSet().for_("MEYLE"), b)                   # однакова конкретність: менший пріоритет
        self.assertIsNone(PriceRule.objects.all().delete() and pricing.RuleSet().for_("X"))

    def test_compute_price_markup_min_margin_and_rounding(self):
        cp = pricing.compute_price
        self.assertEqual(cp(1000, 30), D("1300.00"))
        r = PriceRule(markup_pct=10, min_margin_pct=D("40"), rounding=0)
        self.assertEqual(cp(1000, 10, r), D("1666.67"))                           # поріг маржі 40%: 1000 / 0.6
        self.assertEqual(cp(1000, 80, r), D("1800.00"))                           # націнка вища за поріг: поріг не заважає
        self.assertEqual(cp(1003, 30, PriceRule(rounding=10)), D("1310.00"))      # 1303.9 → вгору до 10
        self.assertEqual(cp(1000, 30, PriceRule(rounding=10)), D("1300.00"))      # рівно круглу не піднімає
        self.assertEqual(cp(1000, 30, PriceRule(rounding=5)), D("1300.00"))
        self.assertEqual(cp(Decimal("12.34"), 30, PriceRule(rounding=1)), D("17.00"))
        self.assertEqual(cp(100, 0, PriceRule(min_margin_pct=D("99.5"))), D("20000.00"))
        self.assertEqual(pricing.margin_pct(1300, 1000).quantize(D("0.1")), D("23.1"))

    def test_base_pct_priority_customer_tier_then_rule_then_default(self):
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        sto = MarkupTier.objects.create(name="СТО", percent=15)
        plain, with_tier = Customer.objects.create(name="A", phone="1"), Customer.objects.create(name="B", phone="2", markup_tier=sto)
        r40, r_none = PriceRule(markup_pct=40), PriceRule(markup_pct=None)
        self.assertEqual(pricing.base_pct(with_tier, r40), 15)                    # умови клієнта важливіші
        self.assertEqual(pricing.base_pct(plain, r40), 40)                        # правило замість типової
        self.assertEqual(pricing.base_pct(None, r_none), 30)                      # правило без націнки → типова
        self.assertEqual(pricing.base_pct(None, None), 30)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class PricingFlowTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("anna", password="x")
        self.adm, self.anna = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.anna.login(username="anna", password="x")
        self.anna.post("/shift/", {"action": "open", "cash": "0"})
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        self.sto = MarkupTier.objects.create(name="СТО", percent=15)
        self.sup = Supplier.objects.create(name="Busmarket")
        self.si = SupplierItem.objects.create(supplier=self.sup, brand="BILSTEIN", sku="19-152615", sku_norm="19152615", cost=1000, name="Амортизатор")

    def checkout(self, items, customer=None):
        r = self.anna.post("/pos/checkout/", json.dumps({"items": items, "payment": "cash", "customer_id": customer.pk if customer else None}),
                           content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        from core.models import Sale
        return Sale.objects.get(pk=r.json()["id"]).items.get()

    def test_pos_uses_rule_for_supplier_items_server_side_and_exposes_it(self):
        rule(brand="BILSTEIN", markup_pct=40, rounding=10)
        d = self.anna.get("/pos/lookup/", {"q": "19152615"}).json()["suppliers"][0]
        self.assertEqual(d["rl"], {"mk": 40.0, "mm": 0.0, "rd": 10})
        self.assertEqual(self.checkout([{"si": self.si.pk, "qty": 1}]).price, D("1400.00"))               # правило: +40%
        cust = Customer.objects.create(name="СТО", phone="3", markup_tier=self.sto)
        self.assertEqual(self.checkout([{"si": self.si.pk, "qty": 1}], cust).price, D("1150.00"))        # умови клієнта: +15%
        PriceRule.objects.update(min_margin_pct=40)
        self.assertEqual(self.checkout([{"si": self.si.pk, "qty": 1}], cust).price, D("1670.00"))        # поріг маржі підняв ціну навіть СТО
        self.assertEqual(self.checkout([{"si": self.si.pk, "qty": 1, "price": 1234.5}]).price, D("1234.50"))   # ручна ціна лишається

    def test_auto_price_follows_purchases_but_manual_products_are_untouched(self):
        rule(markup_pct=50, rounding=10)
        auto = Product.objects.create(name="A", sku="A1", brand="X", cost_price=100, price=130, auto_price=True)
        manual = Product.objects.create(name="B", sku="B1", brand="X", cost_price=100, price=130)
        services.create_purchase(get_user_model().objects.get(username="admin"), self.sup, [(auto, D(2), D(200)), (manual, D(2), D(200))])
        auto.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual((auto.cost_price, auto.price), (D("200"), D("300.00")))                 # 200 +50%
        self.assertEqual((manual.cost_price, manual.price), (D("200"), D("130")))               # без автоціни: ціна не змінилась
        h = PriceHistory.objects.filter(product=auto, field="price").get()
        self.assertEqual((h.old_value, h.new_value), (D("130"), D("300.00")))

    def test_supplier_price_import_logs_changes_and_reprices_auto_products(self):
        rule(markup_pct=30)
        mine = Product.objects.create(name="Амортизатор", sku="19 152 615", brand="BILSTEIN", cost_price=900, price=1170, auto_price=True)
        other = Product.objects.create(name="Інший", sku="X-1", brand="NGK", cost_price=10, price=13, auto_price=True)
        SupplierItem.objects.create(supplier=self.sup, brand="NGK", sku="X-1", sku_norm="X1", cost=10)
        cheaper = Supplier.objects.create(name="Дешевший")
        SupplierItem.objects.create(supplier=cheaper, brand="BILSTEIN", sku="19-152615", sku_norm="19152615", cost=1100)
        rows = [["Бренд", "Артикул", "Назва", "Ціна"], ["BILSTEIN", "19-152615", "Амортизатор", 1200], ["NGK", "X-1", "Свічка", 10]]
        f = SimpleUploadedFile("p.xlsx", xlsx_bytes(rows))
        r = self.adm.post("/supplier-prices/map/", {"supplier": self.sup.pk, "file": f, "header_row": 1})
        ctx = r.context
        data = {"supplier": self.sup.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": 1}
        data.update({f"col_{x['key']}": x["sel"] for x in ctx["fields"] if x["sel"] is not None})
        self.adm.post("/supplier-prices/apply/", data)
        ch = SupplierPriceChange.objects.get()                                                    # змінилась лише одна ціна
        self.assertEqual((ch.item, ch.old_cost, ch.new_cost), (self.si, D("1000"), D("1200")))
        mine.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(mine.price, D("1430.00"))          # від найдешевшої доступної закупівлі 1100 (+30%), а не від 1200
        self.assertEqual(mine.cost_price, D("900"))         # собівартість залишку не міняється
        self.assertEqual(other.price, D("13"))              # ціна НЕ змінилась: перерахунок лише для змінених позицій
        self.assertContains(self.adm.get("/price-review/"), "1000")

    def test_review_page_flags_applies_and_toggles_auto(self):
        rule(markup_pct=30, min_margin_pct=20)
        low = Product.objects.create(name="Дешево", sku="L1", brand="B", cost_price=100, price=110)     # маржа 9%
        ok = Product.objects.create(name="Норм", sku="O1", brand="B", cost_price=100, price=130)
        SupplierItem.objects.create(supplier=self.sup, brand="B", sku="O1", sku_norm="O1", cost=150)      # закупівля зросла
        r = self.adm.get("/price-review/")
        rows = {x["p"].sku: x for x in r.context["rows"]}
        self.assertEqual(set(rows), {"L1", "O1"})
        self.assertTrue(rows["L1"]["low"] and rows["L1"]["below"])
        self.assertTrue(rows["O1"]["cost_up"])
        self.assertEqual(rows["O1"]["rec"], D("195.00"))                                          # від замінної закупівлі 150 (+30%)
        self.assertEqual((r.context["totals"]["needs"], r.context["totals"]["low"], r.context["totals"]["cost_up"]), (2, 1, 1))
        self.adm.post("/price-review/", {"act": "apply", "ids": [low.pk]})
        low.refresh_from_db()
        self.assertEqual(low.price, D("130.00"))
        self.assertEqual(Product.objects.get(pk=ok.pk).price, D("130"))                           # невибране не змінилось
        self.adm.post("/price-review/", {"act": "apply_all"})
        self.assertEqual(Product.objects.get(pk=ok.pk).price, D("195.00"))
        self.adm.post("/price-review/", {"act": "enable_auto", "ids": [low.pk]})
        self.assertTrue(Product.objects.get(pk=low.pk).auto_price)
        self.assertEqual(self.anna.get("/price-review/").status_code, 403)
        self.assertEqual(self.anna.post("/price-review/", {"act": "apply_all"}).status_code, 403)

    def test_rules_pages_validation_and_product_form_auto_price(self):
        r = self.adm.post("/price-rules/new/", {"name": "x", "brand": "bilstein", "markup_pct": "40", "min_margin_pct": "120",
                                                "rounding": 0, "priority": 100, "is_active": "on"})
        self.assertContains(r, "від 0 до 99")
        self.adm.post("/price-rules/new/", {"name": "Bilstein", "brand": "bilstein", "markup_pct": "40", "min_margin_pct": "10",
                                            "rounding": 10, "priority": 100, "is_active": "on"})
        pr = PriceRule.objects.get()
        self.assertEqual(pr.brand, "BILSTEIN")
        self.assertContains(self.adm.get("/price-rules/"), "1400,00")                             # приклад розрахунку на 1000 ₴
        self.assertEqual(self.adm.get(f"/price-rules/{pr.pk}/edit/").status_code, 200)
        self.assertEqual(self.anna.get("/price-rules/").status_code, 403)
        data = {"brand": "bilstein", "sku": "Z-9", "name": "Деталь", "unit": "шт", "cost_price": "1000", "price": "1", "min_stock": "0",
                "initial_stock": "0", "is_active": "on", "auto_price": "on"}
        r = self.adm.post("/products/new/", data, follow=True)
        self.assertContains(r, "перераховано за правилами")
        self.assertEqual(Product.objects.get(sku="Z-9").price, D("1400.00"))                      # введена вручну «1» замінена правилом


@unittest.skipUnless(shutil.which("node"), "node не встановлено")
class JsParityTest(TestCase):
    """Формула в браузері (pos.html) має давати ті самі ціни, що й сервер."""

    def test_js_formula_matches_python(self):
        html = open("core/templates/core/pos.html", encoding="utf-8").read()
        m = re.search(r"const sp=\(c,p,rl\)=>\{.*?return Math\.round\(x\*100\)/100\};", html)
        self.assertIsNotNone(m)
        cases = [(1000, 30, None), (1003, 30, dict(mm=0, rd=10)), (1000, 10, dict(mm=40, rd=0)), (1000, 10, dict(mm=40, rd=10)),
                 (12.34, 30, dict(mm=0, rd=1)), (999.99, 25, dict(mm=15, rd=5)), (50, 0, dict(mm=33.3, rd=0)), (333.33, 17.5, dict(mm=0, rd=5))]
        js = m.group(0) + f"\nconst cases={json.dumps(cases)};console.log(JSON.stringify(cases.map(c=>sp(c[0],c[1],c[2]))));"
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        for (cost, pct, rl), got in zip(cases, out):
            rr = PriceRule(min_margin_pct=D(str(rl["mm"])), rounding=rl["rd"]) if rl else None
            want = pricing.compute_price(D(str(cost)), D(str(pct)), rr)
            self.assertAlmostEqual(float(want), got, places=2, msg=f"{cost} {pct} {rl}")
