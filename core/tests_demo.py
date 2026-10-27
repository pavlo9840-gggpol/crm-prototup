import csv
import os
import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from core import crosses
from core.models import (CrossRef, Customer, CustomerCar, PartInfo, PartOe, Product, Receipt, Sale, SaleItem, Supplier, SupplierItem)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class DemoDataTest(TestCase):
    """Наскрізна перевірка на демо-даних: прайси → аналоги → клієнти з авто → замовлення → приймання накладної."""

    def setUp(self):
        get_user_model().objects.create_superuser("admin", "", "x")
        self.c = self.client
        self.c.login(username="admin", password="x")
        self.out = os.path.join(tempfile.mkdtemp(), "inv.csv")
        call_command("load_demo_data", out=self.out, stdout=StringIO())

    def test_dataset_shape_and_idempotence(self):
        self.assertGreater(SupplierItem.objects.count(), 200)
        self.assertGreater(PartOe.objects.count(), 100)
        self.assertEqual((Customer.objects.filter(notes="DEMO").count(), CustomerCar.objects.count(), Sale.objects.count()), (3, 4, 2))
        before = (Product.objects.count(), SupplierItem.objects.count())
        buf = StringIO()
        call_command("load_demo_data", out=self.out, stdout=buf)
        self.assertIn("вже створено", buf.getvalue())
        self.assertEqual((Product.objects.count(), SupplierItem.objects.count()), before)

    def test_exact_analogs_by_oe_and_name_inference_on_demo_parts(self):
        o = PartOe.objects.filter(brand="BILSTEIN").first()
        res = crosses.analogs(o.brand, o.sku_norm)
        self.assertGreaterEqual(len(res), 2)                                              # інші бренди з тим самим OE
        self.assertTrue(all(r["level"] in ("oe", "manual") and r["score"] >= 70 for r in res))
        self.assertTrue(all(r["brand"] != "BILSTEIN" for r in res))
        no_oe = next(i for i in SupplierItem.objects.filter(brand="BILSTEIN") if not PartOe.objects.filter(brand=i.brand, sku_norm=i.sku_norm).exists())
        named = crosses.analogs(no_oe.brand, no_oe.sku_norm, with_names=True)
        self.assertTrue(named and all(r["level"] == "name" and r["score"] <= 40 for r in named))
        # без назв (як на касі) таких підказок немає: назви не підміняють точні дані
        self.assertEqual(crosses.analogs(no_oe.brand, no_oe.sku_norm), [])
        card = self.c.get("/part/", {"b": o.brand, "s": o.sku_norm})
        self.assertContains(card, "Точний аналог (спільний OE)")
        self.assertContains(card, "Тестовий опис")

    def test_pos_search_with_customer_car_marks_fitting_parts(self):
        car = CustomerCar.objects.get(plate="AA 1234 BB")                                    # Renault Trafic II
        d = self.c.get("/pos/lookup/", {"q": "амортизатор renault", "car": car.pk}).json()
        fits = {s["fit"] for s in d["suppliers"]}
        self.assertTrue(fits & {True})                                                       # є позиції для цього авто
        self.assertNotIn(None, fits)                                                         # для всіх демо-деталей застосовність відома
        opel = d["suppliers"][0]
        other = CustomerCar.objects.get(plate="KA 5555 CE")
        d2 = self.c.get("/pos/lookup/", {"q": "амортизатор renault", "car": other.pk}).json()   # Opel Vivaro
        self.assertEqual({s["fit"] for s in d2["suppliers"]}, {False})
        self.assertEqual(self.c.get("/pos/customer/", {"q": "aa1234bb"}).json()["results"][0]["name"], "Іван Петренко (демо)")

    def test_receiving_generated_invoice_end_to_end(self):
        bm = Supplier.objects.get(name="Busmarket (демо)")
        with open(self.out, "rb") as f:
            r = self.c.post("/receive/preview/", {"supplier": bm.pk, "file": SimpleUploadedFile("inv.csv", f.read()), "number": "DEMO-1"})
        t = r.context["plan"]["totals"]
        self.assertEqual((t["matched"], t["stock_lines"], t["errors"]), (2, 1, 0))           # 2 замовлення клієнтів + 1 на склад
        ctx = r.context
        data = {"supplier": bm.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": ctx["hdr"], "number": "DEMO-1", "days": "14"}
        data.update({f"col_{x['key']}": x["sel"] for x in ctx["fields"] if x["sel"] is not None})
        self.assertContains(self.c.post("/receive/apply/", data, follow=True), "під клієнтів 2")
        self.assertEqual(SaleItem.objects.filter(source="supplier", fulfil_status="arrived").count(), 2)
        self.assertEqual(Receipt.objects.get().n_stock, 1)
        self.assertContains(self.c.get("/orders/?st=arrived"), "Іван Петренко (демо)")
        self.assertContains(self.c.get("/orders/?st=arrived"), "🚗")                            # авто клієнта видно в замовленні

    def test_reset_removes_everything_demo(self):
        call_command("load_demo_data", reset=True, stdout=StringIO())
        for model in (SupplierItem, PartOe, PartInfo, CrossRef, CustomerCar, Sale, SaleItem, Customer, Product):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertFalse(Supplier.objects.exists())
        call_command("load_demo_data", reset=True, stdout=StringIO())                       # повторно безпечно
        call_command("load_demo_data", out=self.out, stdout=StringIO())                      # і можна створити знову
        self.assertGreater(SupplierItem.objects.count(), 200)

    def test_reset_after_receiving_keeps_protected_data_without_crashing(self):
        bm = Supplier.objects.get(name="Busmarket (демо)")
        with open(self.out, "rb") as f:
            r = self.c.post("/receive/preview/", {"supplier": bm.pk, "file": SimpleUploadedFile("inv.csv", f.read())})
        ctx = r.context
        data = {"supplier": bm.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": ctx["hdr"], "days": "14"}
        data.update({f"col_{x['key']}": x["sel"] for x in ctx["fields"] if x["sel"] is not None})
        self.c.post("/receive/apply/", data)
        buf = StringIO()
        call_command("load_demo_data", reset=True, stdout=buf)                               # закупівля посилається на постачальника
        self.assertIn("не видалено", buf.getvalue())
        self.assertEqual(Customer.objects.filter(notes="DEMO").count(), 0)
