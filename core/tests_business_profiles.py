from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from core.business_profiles import (
    AUTO_PARTS_NAV_ITEMS,
    dashboard_profile_config,
    navigation_items_for_profile,
    profile_allows_path,
    required_profile_for_path,
)
from core.tenant_context import BusinessProfileMiddleware


class BusinessProfileNavigationTests(SimpleTestCase):
    PROFILES = ("general_retail", "grocery", "auto_parts", "electronics", "online_store")

    def test_each_supported_profile_has_distinct_dashboard_identity(self):
        configs = {profile: dashboard_profile_config(profile) for profile in self.PROFILES}
        self.assertEqual(set(configs), set(self.PROFILES))
        self.assertEqual(len({item["title"] for item in configs.values()}), len(self.PROFILES))
        self.assertEqual(dashboard_profile_config("unknown")["title"], configs["general_retail"]["title"])

    def test_automotive_navigation_is_visible_only_for_auto_parts(self):
        items = [
            ("Товари", "product_list"),
            ("Підбір за авто", "car_parts"),
            ("Каталог запчастин", "catalogue_home"),
            ("Аналоги", "crossrefs"),
            ("OE-номери", "parts_import"),
        ]
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                visible = navigation_items_for_profile(items, profile)
                visible_names = {name for _label, name in visible}
                if profile == "auto_parts":
                    self.assertTrue(AUTO_PARTS_NAV_ITEMS.issubset(visible_names))
                else:
                    self.assertTrue(AUTO_PARTS_NAV_ITEMS.isdisjoint(visible_names))
                self.assertIn("product_list", visible_names)

    def test_tracking_tabs_appear_only_when_products_use_that_tracking(self):
        items = [
            ("Залишки", "inventory"),
            ("Партії та терміни", "stock_batches"),
            ("Серійні номери / IMEI", "stock_serials"),
        ]
        without_tracking = {name for _label, name in navigation_items_for_profile(
            items, "electronics", tracking_modes=["none"]
        )}
        self.assertEqual(without_tracking, {"inventory"})

        batch_tracking = {name for _label, name in navigation_items_for_profile(
            items, "grocery", tracking_modes=["expiry"]
        )}
        self.assertEqual(batch_tracking, {"inventory", "stock_batches"})

        serial_tracking = {name for _label, name in navigation_items_for_profile(
            items, "electronics", tracking_modes=["serial"]
        )}
        self.assertEqual(serial_tracking, {"inventory", "stock_serials"})

    def test_automotive_paths_are_classified(self):
        paths = (
            "/car-parts/",
            "/catalogue/",
            "/crossrefs/",
            "/parts-data/",
            "/part/",
            "/part/analog/",
            "/pos/car/new/",
            "/customers/17/car/add/",
            "/cars/23/delete/",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(required_profile_for_path(path), "auto_parts")
                self.assertFalse(profile_allows_path("electronics", path))
                self.assertTrue(profile_allows_path("auto_parts", path))

    def test_shared_retail_paths_remain_available_for_every_profile(self):
        for profile in self.PROFILES:
            with self.subTest(profile=profile):
                self.assertTrue(profile_allows_path(profile, "/products/"))
                self.assertTrue(profile_allows_path(profile, "/pos/"))
                self.assertTrue(profile_allows_path(profile, "/finance/"))


class BusinessProfileMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_non_automotive_profile_cannot_open_automotive_url_directly(self):
        request = self.factory.get("/car-parts/")
        request.company = SimpleNamespace(business_type="grocery", get_business_type_display=lambda: "Продуктовий магазин")
        request.user = AnonymousUser()
        request.resolver_match = None
        response = BusinessProfileMiddleware(lambda _request: HttpResponse("ok"))(request)
        self.assertEqual(response.status_code, 403)

    def test_auto_parts_profile_can_open_automotive_url(self):
        request = self.factory.get("/car-parts/")
        request.company = SimpleNamespace(business_type="auto_parts")
        request.user = AnonymousUser()
        response = BusinessProfileMiddleware(lambda _request: HttpResponse("ok"))(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_shared_url_is_not_blocked_for_other_profiles(self):
        request = self.factory.get("/products/")
        request.company = SimpleNamespace(business_type="electronics")
        request.user = AnonymousUser()
        response = BusinessProfileMiddleware(lambda _request: HttpResponse("ok"))(request)
        self.assertEqual(response.status_code, 200)
