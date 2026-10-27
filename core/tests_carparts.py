from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core import fitment
from core.models import Category, Customer, CustomerCar, MarkupTier, PartInfo, PriceRule, Product, Supplier, SupplierItem

D = Decimal


class FitmentTest(TestCase):
    def test_year_ranges_two_and_four_digits_open_ended(self):
        yr = fitment.year_range
        self.assertEqual(yr("renault trafic ii 2001-2014"), (2001, 2014))
        self.assertEqual(yr("opel vivaro 01-"), (2001, 9999))                  # «01-» = з 2001 і далі
        self.assertEqual(yr("ducato 98-06"), (1998, 2006))
        self.assertEqual(yr("renault trafic ii"), None)
        self.assertTrue(fitment.line_matches("renault trafic ii 2001-2014", ["renault", "trafic"], 2008))
        self.assertFalse(fitment.line_matches("renault trafic ii 2001-2014", ["renault", "trafic"], 2018))      # поза діапазоном
        self.assertFalse(fitment.line_matches("renault trafic ii 01-", ["renault", "trafic"], 1999))
        self.assertTrue(fitment.line_matches("renault trafic ii 01-", ["renault", "trafic"], 2020))
        self.assertTrue(fitment.line_matches("renault trafic", ["renault", "trafic"], 1990))                    # років немає: не відсіюємо
        self.assertFalse(fitment.line_matches("renault trafic", ["renault", "master"], 2008))                   # не вся модель збіглась
        self.assertFalse(fitment.line_matches("renault trafic", [], 2008))


class CarPartsTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_user("anna", password="x")
        self.c = self.client
        self.c.login(username="anna", password="x")
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        self.sto = MarkupTier.objects.create(name="СТО", percent=15)
        self.sup = Supplier.objects.create(name="Busmarket")
        self.cust = Customer.objects.create(name="СТО Вікторія", phone="1", markup_tier=self.sto)
        self.car = CustomerCar.objects.create(customer=self.cust, plate="AA1234BB", make="Renault", model="Trafic II", year=2008)
        self.cat = Category.objects.create(name="Підвіска")
        self.add("BILSTEIN", "19-152615", "Амортизатор задній Renault Trafic II", 1000, "Renault Trafic II 2001-2014", own=3, cat=self.cat)
        self.add("SACHS", "315849", "Амортизатор задній Renault Trafic II", 800, "Renault Trafic II 2001-2014")
        self.add("TRW", "GDB1833", "Колодки гальмівні передні Renault Trafic II", 600, "Renault Trafic II 01-")
        self.add("MEYLE", "M-OLD", "Тяга Renault Trafic II", 300, "Renault Trafic II 1995-2000")                  # поза роками авто
        self.add("KYB", "K-OPEL", "Амортизатор Opel Vivaro", 700, "Opel Vivaro 2001-")                              # інше авто
        self.add("NGK", "N-NODATA", "Свічка", 50, "")                                                               # застосовності немає

    def add(self, brand, sku, name, cost, cars, own=None, cat=None):
        SupplierItem.objects.create(supplier=self.sup, brand=brand, sku=sku, sku_norm="".join(c for c in sku if c.isalnum()).upper(), name=name, cost=cost)
        PartInfo.objects.create(brand=brand, sku_norm="".join(c for c in sku if c.isalnum()).upper(), sku=sku, cars=cars)
        if own:
            Product.objects.create(name=name, sku=sku, brand=brand, cost_price=cost, price=1300, current_stock=own, category=cat)

    def get(self, **kw):
        return self.c.get("/car-parts/", kw)

    def test_groups_by_type_filters_by_year_and_model_and_prices_for_customer(self):
        r = self.get(car=self.car.pk)
        groups = {g["name"]: [x["sku"] for x in g["rows"]] for g in r.context["groups"]}
        # групи за типом деталі з назви (власний товар і прайс разом); спершу наявне на складі; без Opel, без 1995-2000, без «немає даних»
        self.assertEqual(groups, {"Амортизатор": ["19-152615", "315849"], "Колодки": ["GDB1833"]})
        self.assertEqual(r.context["total"], 3)
        self.assertEqual([g["name"] for g in r.context["groups"]], ["Амортизатор", "Колодки"])
        self.assertContains(r, "1300")                                      # наша ціна на складі
        row = {x["sku"]: x for g in r.context["groups"] for x in g["rows"]}
        self.assertEqual(row["315849"]["offer_price"], D("920.00"))         # 800 +15% (умови СТО), не +30%
        self.assertEqual(row["GDB1833"]["offer_price"], D("690.00"))
        PriceRule.objects.create(name="мін.маржа", min_margin_pct=40, rounding=10)
        row = {x["sku"]: x for g in self.get(car=self.car.pk).context["groups"] for x in g["rows"]}
        self.assertEqual(row["315849"]["offer_price"], D("1340.00"))        # 800 / 0.6 = 1333.33 → до 10 вгору
        self.assertContains(self.get(car=self.car.pk), "для клієнта <b>СТО Вікторія</b>")

    def test_filters_free_text_search_and_empty_states(self):
        r = self.get(make="renault", model="trafic ii", year="2008", q="АМОРТИЗ")          # кирилиця в будь-якому регістрі
        self.assertEqual([g["name"] for g in r.context["groups"]], ["Амортизатор"])
        self.assertEqual(self.get(make="Renault", model="Trafic II", year="2008", instock="1").context["total"], 1)   # лише наявні
        self.assertEqual(self.get(make="Renault", model="Trafic II", year="1998").context["total"], 1)                # інший рік → інший набір
        self.assertEqual(self.get(make="Renault", model="Trafic II", year="abc").context["searched"], True)         # некоректний рік ігнорується
        self.assertContains(self.get(make="Ferrari", model="F40"), "Сумісних деталей не знайдено")
        self.assertContains(self.get(), "Оберіть авто клієнта")
        self.assertEqual(self.c.get("/car-parts/", {"car": "999"}).status_code, 404)
        PartInfo.objects.all().delete()
        self.assertContains(self.get(make="Renault", model="Trafic II"), "немає даних про застосовність")

    def test_links_from_customer_card_and_pos_and_login_required(self):
        adm = self.client_class()
        get_user_model().objects.create_superuser("admin", "", "x")
        adm.login(username="admin", password="x")
        self.assertContains(adm.get(f"/customers/{self.cust.pk}/"), f"/car-parts/?car={self.car.pk}")
        self.c.post("/shift/", {"action": "open", "cash": "0"})
        self.assertIn('id="carparts"', self.c.get("/pos/").content.decode())
        self.assertEqual(self.client_class().get("/car-parts/").status_code, 302)                           # лише для авторизованих
