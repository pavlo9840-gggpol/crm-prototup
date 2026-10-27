import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Category, MarkupTier, PartInfo, PartOe, PriceHistory, Product, Supplier, SupplierItem

D = Decimal


class ProductFormUxTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("cash", password="x")
        self.adm, self.cash = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.cash.login(username="cash", password="x")
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        MarkupTier.objects.create(name="СТО", percent=15)
        self.sup = Supplier.objects.create(name="Busmarket")
        SupplierItem.objects.create(supplier=self.sup, brand="BILSTEIN", sku="19-152615", sku_norm="19152615", cost=1000,
                                    qty_avail=4, name="Амортизатор задній Trafic")
        SupplierItem.objects.create(supplier=self.sup, brand="BILSTEIN", sku="OLD-1", sku_norm="OLD1", cost=5, available=False)

    FORM = {"brand": "bilstein", "sku": "19-152615", "name": "Амортизатор задній", "unit": "шт", "cost_price": "1000",
            "price": "1300", "min_stock": "0", "initial_stock": "2", "is_active": "on"}

    def test_page_structure_and_numbers_are_js_safe(self):
        html = self.adm.get("/products/new/").content.decode()
        for needle in ("Що це за деталь", "Ціни", "Склад", "Зберегти й додати ще", "id=\"mk\"", "brand-list"):
            self.assertIn(needle, html)
        # дробові числа в JS/атрибутах не мають комою (локаль uk), інакше розрахунок ціни зламається
        self.assertEqual(re.findall(r'data-pct="([^"]+)"', html), ["15.000000", "30.000000"])
        self.assertRegex(html, r"const DEF=30\.0+,")
        self.assertEqual(self.cash.get("/products/new/").status_code, 403)

    def test_prefill_from_query_and_from_part_card(self):
        c = Category.objects.create(name="Підвіска")
        html = self.adm.get("/products/new/", {"brand": "BILSTEIN", "sku": "19-152615", "name": "Амортизатор",
                                               "cost_price": "1000.00", "category": c.pk}).content.decode()
        self.assertIn('value="BILSTEIN"', html)
        self.assertIn('value="19-152615"', html)
        self.assertIn('value="1000.00"', html)
        self.assertRegex(html, r'<option value="%d" selected>' % c.pk)
        # кнопка «На склад» у картці запчастини: лише адміну й лише коли товару ще немає
        card = self.adm.get("/part/?b=BILSTEIN&s=19152615")
        self.assertContains(card, "➕ На склад")
        self.assertContains(card, "cost_price=1000.00")
        self.assertNotContains(self.cash.get("/part/?b=BILSTEIN&s=19152615"), "➕ На склад")
        self.adm.post("/products/new/", self.FORM)
        self.assertNotContains(self.adm.get("/part/?b=BILSTEIN&s=19152615"), "➕ На склад")

    def test_save_and_add_another_keeps_brand_category_unit(self):
        c = Category.objects.create(name="Підвіска")
        r = self.adm.post("/products/new/", dict(self.FORM, category=c.pk, again="1"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Product.objects.get().current_stock, 2)
        self.assertIn("brand=BILSTEIN", r["Location"])
        self.assertIn(f"category={c.pk}", r["Location"])
        r2 = self.adm.get(r["Location"])
        self.assertContains(r2, 'value="BILSTEIN"')
        r = self.adm.post("/products/new/", dict(self.FORM, sku="OTHER-2", name="Інший"))      # звичайне збереження
        self.assertEqual(r["Location"], "/products/")

    def test_edit_page_has_no_initial_stock_and_records_price_history(self):
        self.adm.post("/products/new/", self.FORM)
        p = Product.objects.get()
        html = self.adm.get(f"/products/{p.pk}/edit/").content.decode()
        self.assertNotIn("id_initial_stock", html)
        self.assertNotIn("Зберегти й додати ще", html)
        data = {k: v for k, v in self.FORM.items() if k != "initial_stock"}
        self.adm.post(f"/products/{p.pk}/edit/", dict(data, price="1400"))
        self.assertEqual(PriceHistory.objects.get().new_value, D("1400"))
        self.assertContains(self.adm.get(f"/products/{p.pk}/edit/"), "Історія зміни цін")

    def test_lookup_offers_duplicate_other_brands_and_info(self):
        PartOe.objects.create(brand="BILSTEIN", sku_norm="19152615", oe_norm="1K0411105AB", oe_raw="1K0 411 105 AB", source="api")
        PartOe.objects.create(brand="SACHS", sku_norm="315849", oe_norm="1K0411105AB", oe_raw="1K0411105AB", source="api")
        PartInfo.objects.create(brand="BILSTEIN", sku_norm="19152615", description="Газомасляний амортизатор", source="api")
        d = self.adm.get("/products/lookup/", {"b": "bilstein", "s": "19 152 615"}).json()
        self.assertIsNone(d["dup"])
        o = d["offers"][0]
        self.assertEqual((o["supplier"], o["cost"], o["qty"], o["price"]), ("Busmarket", 1000.0, 4.0, 1300.0))   # +30% за замовчуванням
        self.assertEqual(d["info"]["oe"], 1)
        self.assertEqual(d["info"]["analogs"], 1)
        self.assertIn("Газомасляний", d["info"]["description"])
        self.assertIn("/part/?b=BILSTEIN", d["card"])
        # недоступна позиція прайсу теж повертається (з ознакою), щоб менеджер бачив, що її немає
        self.assertFalse(self.adm.get("/products/lookup/", {"b": "bilstein", "s": "OLD-1"}).json()["offers"][0]["available"])
        # дубль на складі та той самий артикул в іншого бренду
        self.adm.post("/products/new/", self.FORM)
        p = Product.objects.get()
        Product.objects.create(name="Той самий номер, інший виробник", sku="19152615", brand="MEYLE", price=1)
        d = self.adm.get("/products/lookup/", {"b": "BILSTEIN", "s": "19152615"}).json()
        self.assertEqual((d["dup"]["name"], d["dup"]["stock"]), ("Амортизатор задній", 2.0))
        self.assertEqual([x["brand"] for x in d["others"]], ["MEYLE"])
        # під час редагування сам товар дублем не вважається
        self.assertIsNone(self.adm.get("/products/lookup/", {"b": "BILSTEIN", "s": "19152615", "pk": p.pk}).json()["dup"])
        # короткий ввід і права
        self.assertTrue(self.adm.get("/products/lookup/", {"b": "B", "s": "1"}).json()["empty"])
        self.assertEqual(self.cash.get("/products/lookup/", {"b": "B", "s": "19152615"}).status_code, 403)

    def test_new_navigation_groups_and_pages_render(self):
        html = self.adm.get("/").content.decode()
        self.assertIn("Постачальники та каталог", html)
        self.assertIn("burger", html)
        for u in ("/products/", "/supplier-prices/", "/crossrefs/", "/customers/new/", "/orders/", "/my-sales/", "/pos/"):
            self.assertEqual(self.adm.get(u).status_code, 200, u)
        self.assertEqual(self.cash.get("/orders/").status_code, 200)
