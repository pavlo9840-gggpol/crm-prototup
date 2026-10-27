import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Customer, CustomerCar, PartInfo, Product, Sale, Supplier, SupplierItem, norm_plate

VIN = "VF1FL000X12345678"


class CarsTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("anna", password="x")
        self.adm, self.anna = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.anna.login(username="anna", password="x")
        self.anna.post("/shift/", {"action": "open", "cash": "0"})
        self.ivan = Customer.objects.create(name="Іван Петренко", phone="0501112233")
        self.olena = Customer.objects.create(name="Олена", phone="0679998877")
        self.car = CustomerCar.objects.create(customer=self.ivan, plate="AA 1234 BB", make="Renault", model="Trafic II", year=2008, vin=VIN)
        self.p = Product.objects.create(name="Свічка", sku="SV1", brand="NGK", price=100, cost_price=60, current_stock=5)

    def checkout(self, client, customer, car_id):
        r = client.post("/pos/checkout/", json.dumps({"items": [{"id": self.p.pk, "qty": 1}], "payment": "cash",
                                                      "customer_id": customer.pk, "car_id": car_id}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return Sale.objects.get(pk=r.json()["id"])

    def test_plate_normalization_latin_lookalikes_become_cyrillic(self):
        self.assertEqual(norm_plate("AA 1234 BB"), norm_plate("АА-1234-ВВ"))
        self.assertEqual(norm_plate("aa1234bb"), "АА1234ВВ")
        self.assertEqual(CustomerCar.objects.get(pk=self.car.pk).plate_norm, "АА1234ВВ")

    def test_find_customer_by_plate_or_vin_on_the_till(self):
        for q in ("aa1234bb", "АА 1234", "1234 bb"):                                  # латиницею й кирилицею, частково
            res = self.anna.get("/pos/customer/", {"q": q}).json()["results"]
            self.assertEqual([r["name"] for r in res], ["Іван Петренко"], q)
        res = self.anna.get("/pos/customer/", {"q": VIN.lower()}).json()["results"]
        self.assertEqual(res[0]["cars"][0]["vin"], VIN)
        self.assertEqual(res[0]["cars"][0]["title"], "АА 1234 BB · Renault Trafic II 2008".replace("АА", "AA"))
        self.assertEqual(self.anna.get("/pos/customer/", {"q": "zzzz999"}).json()["results"], [])

    def test_quick_add_car_validates_vin_year_and_requires_something(self):
        post = lambda d: self.anna.post("/pos/car/new/", json.dumps(dict({"customer_id": self.olena.pk}, **d)), content_type="application/json")
        ok = post({"plate": "ke 5555 ab", "make": "Opel", "model": "Vivaro", "year": "2011"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["plate"], "KE 5555 AB")
        self.assertEqual(post({"vin": "TOOSHORT"}).status_code, 400)
        self.assertEqual(post({"vin": "VF1FL000I12345678"}).status_code, 400)          # літера I у VIN заборонена
        self.assertEqual(post({"plate": "AB1", "year": "1800"}).status_code, 400)
        self.assertEqual(post({"plate": "AB1", "year": "abc"}).status_code, 400)
        self.assertEqual(post({}).status_code, 400)
        self.assertEqual(post({"vin": VIN.lower(), "make": "Renault"}).json()["vin"], VIN)   # нормалізується до верхнього регістру
        self.assertEqual(self.olena.cars.count(), 2)

    def test_sale_stores_car_and_ignores_foreign_car(self):
        s = self.checkout(self.anna, self.ivan, self.car.pk)
        self.assertEqual(s.car, self.car)
        self.assertContains(self.adm.get(f"/sales/{s.pk}/"), "Renault Trafic II")
        self.assertContains(self.adm.get(f"/sales/{s.pk}/"), VIN)
        s2 = self.checkout(self.anna, self.olena, self.car.pk)                               # авто іншого клієнта
        self.assertIsNone(s2.car)
        s3 = self.checkout(self.anna, self.ivan, None)
        self.assertIsNone(s3.car)
        # у відкладеному чеку авто зберігається і повертається на касу
        r = self.anna.post("/pos/hold/", json.dumps({"items": [{"id": self.p.pk, "qty": 1}], "customer_id": self.ivan.pk, "car_id": self.car.pk}),
                           content_type="application/json")
        held = Sale.objects.get(number=r.json()["number"])
        self.assertEqual(held.car, self.car)
        init = self.anna.get(f"/pos/?resume={held.pk}").context["init"]
        self.assertEqual(init["car"]["id"], self.car.pk)
        self.assertEqual(init["customer"]["cars"][0]["id"], self.car.pk)

    def test_car_shown_in_history_exports_and_customer_card(self):
        self.checkout(self.anna, self.ivan, self.car.pk)
        html = self.adm.get("/my-sales/").content.decode()
        self.assertIn("🚗", html)
        self.assertIn("Renault Trafic II", html)
        csv = self.adm.get("/my-sales/?csv=1").content.decode("utf-8-sig")
        self.assertIn(";Авто", csv.splitlines()[0])
        self.assertIn("Renault Trafic II", csv)
        page = self.adm.get(f"/customers/{self.ivan.pk}/").content.decode()
        self.assertIn(VIN, page)
        self.assertIn("Додати авто", page)
        # додавання й видалення в картці клієнта (є й у менеджера: це робота біля прилавка)
        r = self.anna.post(f"/customers/{self.ivan.pk}/car/add/", {"plate": "bc 7777 aa", "make": "Opel", "model": "Vivaro", "vin": "bad"}, follow=True)
        self.assertContains(r, "17 символів")
        self.anna.post(f"/customers/{self.ivan.pk}/car/add/", {"plate": "bc 7777 aa", "make": "Opel", "model": "Vivaro"})
        self.assertEqual(self.ivan.cars.count(), 2)
        self.assertEqual(self.anna.post(f"/cars/{self.car.pk}/delete/").status_code, 403)       # видаляє лише адміністратор
        self.adm.post(f"/cars/{self.car.pk}/delete/")
        self.assertEqual(self.ivan.cars.count(), 1)
        self.assertEqual(Sale.objects.filter(status="done").count(), 1)                    # історія продажів лишилась
        self.assertIsNone(Sale.objects.get(status="done").car)

    def test_fit_hint_uses_part_applicability_and_only_hints(self):
        s = Supplier.objects.create(name="S")
        for brand, sku in (("SACHS", "315849"), ("MEYLE", "MEY001"), ("KYB", "KYB001")):
            SupplierItem.objects.create(supplier=s, brand=brand, sku=sku, sku_norm=sku, cost=10, name="Амортизатор")
        PartInfo.objects.create(brand="SACHS", sku_norm="315849", cars="Renault Trafic II 2001-2014\nOpel Vivaro 2001-")
        PartInfo.objects.create(brand="MEYLE", sku_norm="MEY001", cars="Fiat Ducato 2006-")
        fit = lambda q, car=None: {x["sku"]: x["fit"] for x in self.anna.get("/pos/lookup/", {"q": q, "car": car or ""}).json()["suppliers"]}
        self.assertEqual(fit("315849", self.car.pk), {"315849": True})                      # Renault Trafic є в застосовності
        self.assertEqual(fit("MEY001", self.car.pk), {"MEY001": False})                             # дані є, а цього авто в них немає
        self.assertEqual(fit("KYB001", self.car.pk), {"KYB001": None})                              # даних немає: не вгадуємо
        self.assertEqual(fit("315849"), {"315849": None})                                   # авто не вибрано
        self.assertEqual(fit("315849", "9999"), {"315849": None})                           # неіснуюче авто
        noname = CustomerCar.objects.create(customer=self.ivan, plate="XX0000XX")
        self.assertEqual(fit("315849", noname.pk), {"315849": None})                        # у авто немає марки/моделі
