from types import SimpleNamespace

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from core.business_profiles import (
    AUTO_PARTS_NAV_ITEMS,
    navigation_items_for_profile,
    profile_allows_path,
    required_profile_for_path,
)
from core.tenant_context import BusinessProfileMiddleware


class BusinessProfileNavigationTests(SimpleTestCase):
    PROFILES = ("general_retail", "grocery", "auto_parts", "electronics", "online_store")

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
        request.company = SimpleNamespace(business_type="grocery")
        response = BusinessProfileMiddleware(lambda _request: HttpResponse("ok"))(request)
        self.assertEqual(response.status_code, 403)

    def test_auto_parts_profile_can_open_automotive_url(self):
        request = self.factory.get("/car-parts/")
        request.company = SimpleNamespace(business_type="auto_parts")
        response = BusinessProfileMiddleware(lambda _request: HttpResponse("ok"))(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_shared_url_is_not_blocked_for_other_profiles(self):
        request = self.factory.get("/products/")
        request.company = SimpleNamespace(business_type="electronics")
        response = BusinessProfileMiddleware(lambda _request: HttpResponse("ok"))(request)
        self.assertEqual(response.status_code, 200)
