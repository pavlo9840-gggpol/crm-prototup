import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core import apisync, currency
from core.apisync import SyncError
from core.models import (Currency, ExchangeRate, MarkupTier, PriceRule, Product, Supplier, SupplierApi, SupplierItem, SupplierPriceChange)
from core.tests_transit import xlsx_bytes

D = Decimal
TODAY = lambda: timezone.localdate()


def rate(code, value, days_ago=0, source="manual"):
    return ExchangeRate.objects.create(currency=Currency.objects.get(code=code), date=TODAY() - timedelta(days=days_ago), rate=D(str(value)), source=source)


class RatesTest(TestCase):
    def test_seeded_currencies_and_rate_lookup_by_date(self):
        self.assertEqual(list(Currency.objects.values_list("code", flat=True)), ["UAH", "EUR", "PLN", "USD"])
        self.assertTrue(Currency.objects.get(code="UAH").is_base)
        self.assertEqual(currency.get_rate("UAH"), 1)
        with self.assertRaises(currency.RateError) as cm:
            currency.get_rate("EUR")
        self.assertIn("Немає курсу EUR", str(cm.exception))
        rate("EUR", 50, days_ago=10)
        rate("EUR", 51, days_ago=2)
        self.assertEqual(currency.get_rate("EUR"), D("51"))
        self.assertEqual(currency.get_rate("EUR", TODAY() - timedelta(days=5)), D("50"))
        with self.assertRaises(currency.RateError):
            currency.get_rate("EUR", TODAY() - timedelta(days=30))
        self.assertFalse(currency.is_stale(ExchangeRate.objects.get(rate=51)))
        self.assertTrue(currency.is_stale(ExchangeRate.objects.get(rate=50)))
        self.assertTrue(currency.is_stale(None))

    def test_effective_rate_official_margin_fixed_and_hryvnia(self):
        rate("EUR", 50)
        eur = Currency.objects.get(code="EUR")
        uah = Supplier.objects.create(name="Грн")
        official = Supplier.objects.create(name="Євро", currency=eur, rate_margin_pct=D("2"))
        fixed = Supplier.objects.create(name="Свій курс", currency=eur, fixed_rate=D("52.5"), rate_margin_pct=D("5"))
        self.assertEqual(currency.effective_rate(uah), 1)
        self.assertEqual(currency.effective_rate(official), D("51.000000"))
        self.assertEqual(currency.effective_rate(fixed), D("52.500000"))
        self.assertEqual(currency.invoice_rate(official, "EUR"), D("51"))
        self.assertEqual(currency.invoice_rate(official, "UAH"), 1)
        rate("USD", 43)
        self.assertEqual(currency.invoice_rate(official, "USD"), D("43"))
        self.assertEqual(currency.to_uah(D("19.99"), D("51")), D("1019.49"))
        self.assertEqual(currency.to_uah(D("0.005"), D("1")), D("0.01"))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="retail_test_media_"))
class CurrencyFlowTest(TestCase):
    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("anna", password="x")
        self.adm, self.anna = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.anna.login(username="anna", password="x")
        MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
        self.eur = Currency.objects.get(code="EUR")
        self.sup = Supplier.objects.create(name="Euro-Parts", currency=self.eur, rate_margin_pct=D("2"))
        self.uah_sup = Supplier.objects.create(name="Грн-постачальник")
        rate("EUR", 50)

    def import_price(self, sup, rows):
        f = SimpleUploadedFile("p.xlsx", xlsx_bytes([["Бренд", "Артикул", "Назва", "Ціна"]] + rows))
        r = self.adm.post("/supplier-prices/map/", {"supplier": sup.pk, "file": f, "header_row": 1})
        ctx = r.context
        data = {"supplier": sup.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": 1}
        data.update({f"col_{x['key']}": x["sel"] for x in ctx["fields"] if x["sel"] is not None})
        return r, self.adm.post("/supplier-prices/apply/", data, follow=True)

    def test_import_converts_eur_prices_stores_original_and_logs_real_changes_only(self):
        r, resp = self.import_price(self.sup, [["BILSTEIN", "19-152615", "Амортизатор", 20], ["SACHS", "315849", "Амортизатор", "19,99"]])
        self.assertContains(r, "переведені в гривню за курсом")
        self.assertContains(r, "51,0000")
        a = SupplierItem.objects.get(sku="19-152615")
        self.assertEqual((a.price_orig, a.cost), (D("20"), D("1020.00")))
        self.assertEqual(SupplierItem.objects.get(sku="315849").cost, D("1019.49"))
        self.import_price(self.sup, [["BILSTEIN", "19-152615", "Амортизатор", 20], ["SACHS", "315849", "Амортизатор", "19,99"]])
        self.assertEqual(SupplierPriceChange.objects.count(), 0)
        self.import_price(self.sup, [["BILSTEIN", "19-152615", "Амортизатор", 21], ["SACHS", "315849", "Амортизатор", "19,99"]])
        ch = SupplierPriceChange.objects.get()
        self.assertEqual((ch.old_cost, ch.new_cost), (D("1020.00"), D("1071.00")))
        self.import_price(self.uah_sup, [["MEYLE", "M1-1", "Тяга", 300]])
        m = SupplierItem.objects.get(sku="M1-1")
        self.assertEqual((m.cost, m.price_orig), (D("300.00"), None))

    def test_rate_change_recomputes_prices_and_auto_price_but_not_fixed_or_history(self):
        PriceRule.objects.create(name="r", markup_pct=30)
        self.import_price(self.sup, [["BILSTEIN", "19-152615", "Амортизатор", 20]])
        fixed = Supplier.objects.create(name="Свій курс", currency=self.eur, fixed_rate=D("40"))
        SupplierItem.objects.create(supplier=fixed, brand="X", sku="F1", sku_norm="F1", cost=D("800"), price_orig=D("20"))
        auto = Product.objects.create(name="Амортизатор", sku="19-152615", brand="BILSTEIN", cost_price=900, price=1170, auto_price=True)
        manual = Product.objects.create(name="Ручна ціна", sku="X-2", brand="BILSTEIN", cost_price=900, price=1170)
        changed, n = currency.save_rate("EUR", D("52"), TODAY())
        self.assertEqual((changed, n), (True, 1))
        self.assertEqual(SupplierItem.objects.get(sku="19-152615").cost, D("1060.80"))
        self.assertEqual(SupplierItem.objects.get(sku="F1").cost, D("800"))
        auto.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(auto.price, D("1379.04"))
        self.assertEqual(manual.price, D("1170"))
        self.assertEqual(SupplierPriceChange.objects.count(), 0)
        self.assertEqual(currency.save_rate("EUR", D("52"), TODAY()), (False, 0))
        old_cost = SupplierItem.objects.get(sku="19-152615").cost
        changed, n = currency.save_rate("EUR", D("30"), TODAY() - timedelta(days=20))
        self.assertEqual((changed, n), (True, 0))
        self.assertEqual(SupplierItem.objects.get(sku="19-152615").cost, old_cost)

    def test_import_without_rate_is_refused_cleanly(self):
        ExchangeRate.objects.all().delete()
        r, resp = self.import_price(self.sup, [["BILSTEIN", "19-152615", "Амортизатор", 20]])
        self.assertContains(r, "Немає курсу EUR")
        self.assertContains(resp, "Немає курсу EUR")
        self.assertEqual(SupplierItem.objects.count(), 0)

    def test_api_sync_for_foreign_supplier_converts_and_reports_missing_rate(self):
        SupplierApi.objects.create(supplier=self.sup, url="https://x/api", fmt="json", auth_mode="none", items_path="items",
                                   f_sku="a", f_brand="b", f_name="n", f_cost="p")
        net = lambda url, headers: json.dumps({"items": [{"a": "X-1", "b": "Meyle", "n": "Тяга", "p": 10}]}).encode()
        apisync.sync_supplier(Supplier.objects.select_related("api", "currency").get(pk=self.sup.pk), None, fetch=net)
        self.assertEqual(SupplierItem.objects.get().cost, D("510.00"))
        ExchangeRate.objects.all().delete()
        with self.assertRaises(SyncError) as cm:
            apisync.sync_supplier(Supplier.objects.select_related("api", "currency").get(pk=self.sup.pk), None, fetch=net)
        self.assertIn("Немає курсу", str(cm.exception))

    def test_receiving_invoice_in_euro_and_override_currency(self):
        from core.tests_receive import HEAD
        rate("USD", 43)

        def preview(**kw):
            f = SimpleUploadedFile("i.xlsx", xlsx_bytes([HEAD, ["MEYLE", "NEW-1", "Нова", 2, 10]]))
            return self.adm.post("/receive/preview/", dict({"supplier": self.sup.pk, "file": f}, **kw))
        r = preview()
        self.assertContains(r, "10,00 EUR →")
        self.assertEqual(r.context["plan"]["rows"][0]["line"]["cost"], D("510.00"))
        self.assertEqual(r.context["inv_cur"], "EUR")
        self.assertEqual(preview(inv_cur="USD").context["plan"]["rows"][0]["line"]["cost"], D("430.00"))
        self.assertEqual(preview(inv_cur="UAH").context["plan"]["rows"][0]["line"]["cost"], D("10.00"))
        ctx = r.context
        data = {"supplier": self.sup.pk, "token": ctx["token"], "fname": ctx["fname"], "header_row": ctx["hdr"], "inv_cur": "EUR", "number": "E-1"}
        data.update({f"col_{x['key']}": x["sel"] for x in ctx["fields"] if x["sel"] is not None})
        self.adm.post("/receive/apply/", data)
        p = Product.objects.get(sku="NEW-1")
        self.assertEqual((p.current_stock, p.cost_price), (D("2"), D("510.00")))
        ExchangeRate.objects.filter(currency__code="USD").delete()
        self.assertEqual(preview(inv_cur="USD").status_code, 302)
        self.assertContains(self.adm.get("/receive/"), "Немає курсу USD")

    def test_part_card_shows_original_price_and_supplier_form(self):
        self.import_price(self.sup, [["BILSTEIN", "19-152615", "Амортизатор", 20]])
        self.assertContains(self.adm.get("/part/?b=BILSTEIN&s=19152615"), "20,00 EUR")
        data = {"name": "Новий", "currency": self.eur.pk, "rate_margin_pct": "", "fixed_rate": ""}
        self.assertEqual(self.adm.post("/suppliers/new/", data).status_code, 302)
        self.assertEqual(Supplier.objects.get(name="Новий").rate_margin_pct, 0)
        self.assertContains(self.adm.post("/suppliers/new/", dict(data, name="Мінус", fixed_rate="-5")), "більшим за 0")


class RatesPageTest(TestCase):
    NBU = [{"r030": 978, "txt": "Євро", "rate": 50.8123, "cc": "EUR", "exchangedate": "21.10.2026"}]

    def setUp(self):
        U = get_user_model()
        U.objects.create_superuser("admin", "", "x")
        U.objects.create_user("anna", password="x")
        self.adm, self.anna = self.client, self.client_class()
        self.adm.login(username="admin", password="x")
        self.anna.login(username="anna", password="x")

    def fake(self, payloads):
        def f(url, headers):
            code = url.split("valcode=")[1][:3]
            p = payloads.get(code)
            if isinstance(p, Exception):
                raise p
            return json.dumps(p if p is not None else []).encode()
        return f

    def test_nbu_fetch_parses_saves_and_reports(self):
        net = self.fake({"EUR": self.NBU, "USD": [{"cc": "USD", "rate": 43.5, "exchangedate": "21.10.2026"}], "PLN": [{"cc": "PLN", "rate": "11.9", "exchangedate": "21.10.2026"}]})
        msg = currency.refresh_from_nbu(None, fetch=net)
        self.assertIn("EUR 50.8123", msg)
        self.assertIn("USD 43.5", msg)
        r = ExchangeRate.objects.get(currency__code="EUR")
        self.assertEqual((r.rate, r.source, r.date.isoformat()), (D("50.812300"), "nbu", "2026-10-21"))
        with self.assertRaises(currency.RateError):
            currency.refresh_from_nbu(None, fetch=self.fake({}))
        with self.assertRaises(currency.RateError) as cm:
            currency.refresh_from_nbu(None, fetch=self.fake({"EUR": SyncError("Постачальник відповів HTTP 503: помилка сервера")}))
        self.assertIn("Не вдалося отримати курси НБУ", str(cm.exception))
        with self.assertRaises(currency.RateError):
            currency.refresh_from_nbu(None, fetch=lambda url, headers: b"<html>not json</html>")

    def test_page_manual_rates_validation_permissions_and_chip(self):
        self.assertEqual(self.anna.get("/rates/").status_code, 403)
        self.assertEqual(self.anna.post("/rates/", {"act": "manual", "code": "EUR", "rate": "50"}).status_code, 403)
        self.assertContains(self.adm.get("/rates/"), "немає курсу")
        self.assertNotContains(self.adm.get("/"), 'class="rate"')
        post = lambda **kw: self.adm.post("/rates/", dict({"act": "manual", "code": "EUR", "rate": "50,80", "date": TODAY().isoformat()}, **kw), follow=True)
        self.assertContains(post(), "Курс EUR збережено: 50.8")
        self.assertEqual(ExchangeRate.objects.get().rate, D("50.8"))
        self.assertContains(post(rate="abc"), "числом")
        self.assertContains(post(rate="-3"), "більшим за 0")
        self.assertContains(post(date=(TODAY() + timedelta(days=9)).isoformat()), "в майбутньому")
        self.assertContains(post(code="XXX"), "Невідома валюта")
        self.assertContains(post(code="UAH"), "Невідома валюта")
        self.assertContains(self.adm.post("/rates/", {"act": "add_currency", "code": "gbp", "name": "Фунт"}, follow=True), "GBP")
        self.assertTrue(Currency.objects.filter(code="GBP").exists())
        self.assertContains(self.adm.post("/rates/", {"act": "add_currency", "code": "G1"}, follow=True), "3 латинські літери")
        self.assertIn("EUR 50,80", self.adm.get("/").content.decode())
        self.assertNotIn("EUR 50,80", self.anna.get("/pos/").content.decode())
        ExchangeRate.objects.update(date=TODAY() - timedelta(days=9))
        self.assertIn("Курс застарів", self.adm.get("/").content.decode())

    def test_page_nbu_button_and_command(self):
        from unittest import mock
        net = self.fake({"EUR": self.NBU})
        with mock.patch("core.apisync.fetch_bytes", net):
            self.assertContains(self.adm.post("/rates/", {"act": "nbu"}, follow=True), "Курси НБУ оновлено")
            buf = StringIO()
            call_command("update_rates", stdout=buf)
            self.assertIn("EUR 50.8123", buf.getvalue())
        with mock.patch("core.apisync.fetch_bytes", self.fake({})):
            self.assertContains(self.adm.post("/rates/", {"act": "nbu"}, follow=True), "НБУ не повернув курсів")
            with self.assertRaises(SystemExit):
                call_command("update_rates", stdout=StringIO(), stderr=StringIO())
        self.assertContains(self.adm.post("/rates/", {"act": "recompute"}, follow=True), "перераховано")
