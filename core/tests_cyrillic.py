import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Customer, Product, Supplier, SupplierItem


class CyrillicSearchTest(TestCase):
    """SQLite порівнює регістр лише для латиниці: пошук «амортизатор» не знаходив «Амортизатор»."""

    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        self.c = self.client
        self.c.login(username="admin", password="x")
        self.c.post("/shift/", {"action": "open", "cash": "0"})
        self.p = Product.objects.create(name="Амортизатор задній Renault", sku="A-1", brand="SACHS", price=100, current_stock=2)
        self.cust = Customer.objects.create(name="Іван Петренко", phone="0501112233")
        s = Supplier.objects.create(name="S")
        SupplierItem.objects.create(supplier=s, brand="MEYLE", sku="M1", sku_norm="M1", name="Колодки Гальмівні ПЕРЕДНІ", cost=5)

    def test_all_searches_ignore_case_for_cyrillic(self):
        for q in ("амортиз", "АМОРТИЗ", "Амортиз", "задній renault"):
            self.assertContains(self.c.get("/products/", {"q": q}), "Амортизатор задній", msg_prefix=q)
            self.assertEqual(len(self.c.get("/pos/search/", {"q": q}).json()["results"]), 1, q)
        for q in ("іван", "ПЕТРЕНКО", "Іван"):
            self.assertEqual([r["name"] for r in self.c.get("/pos/customer/", {"q": q}).json()["results"]], ["Іван Петренко"], q)
            self.assertContains(self.c.get("/customers/", {"q": q}), "Іван Петренко", msg_prefix=q)
        d = self.c.get("/pos/lookup/", {"q": "колодки передні"}).json()                         # прайс постачальника
        self.assertEqual([s["sku"] for s in d["suppliers"]], ["M1"])

    def test_name_lc_is_kept_in_sync_and_migration_backfills(self):
        self.p.name = "Нова НАЗВА"
        self.p.save()
        self.assertEqual(Product.objects.get(pk=self.p.pk).name_lc, "нова назва")
        Product.objects.filter(pk=self.p.pk).update(name="Друга", name_lc="")                      # імітація старих даних
        Customer.objects.update(name_lc="")
        SupplierItem.objects.update(name_lc="")
        mod = importlib.import_module("core.migrations.0012_fill_name_lc")
        mod.fill(apps, None)
        self.assertEqual(Product.objects.get(pk=self.p.pk).name_lc, "друга")
        self.assertEqual(Customer.objects.get().name_lc, "іван петренко")
        self.assertEqual(SupplierItem.objects.get().name_lc, "колодки гальмівні передні")

    def test_price_import_keeps_name_lc(self):
        from core.transit import apply_parsed
        s = Supplier.objects.get(name="S")
        apply_parsed(s, {("X", "X9"): ("X-9", "Свічка ЗАПАЛЮВАННЯ", __import__("decimal").Decimal("10"), None, "")}, None, "t")
        self.assertEqual(SupplierItem.objects.get(sku="X-9").name_lc, "свічка запалювання")
        apply_parsed(s, {("X", "X9"): ("X-9", "Свічка ІРИДІЄВА", __import__("decimal").Decimal("11"), None, "")}, None, "t")
        self.assertEqual(SupplierItem.objects.get(sku="X-9").name_lc, "свічка іридієва")
