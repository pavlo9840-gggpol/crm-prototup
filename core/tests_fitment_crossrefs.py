from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import (
    CatalogPart, CatalogPartNumber, CatalogSource, CrossRef, Customer,
    PartFitment, Product, Supplier, SupplierItem, VehicleMake, VehicleModel,
)


class StructuredFitmentLookupTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("catalog-admin", "catalog@example.test", "secret")
        self.client.force_login(user)
        self.source = CatalogSource.objects.create(name="Fixture fitment source", commercial_use=True)
        self.make = VehicleMake.objects.create(name="Renault", slug="renault", kind="car", source=self.source, name_norm="renault")
        self.model = VehicleModel.objects.create(make=self.make, name="Trafic II", slug="trafic-ii")
        self.part = CatalogPart.objects.create(name="Амортизатор задній")
        self.number = CatalogPartNumber.objects.create(part=self.part, brand="SACHS", brand_norm="sachs",
            sku="315 849", sku_norm="315849", source=self.source, verified=True)
        self.supplier = Supplier.objects.create(name="Parts supplier")
        self.product = Product.objects.create(name="Амортизатор SACHS", brand="SACHS", sku="315 849",
                                              current_stock=2, price=1250)
        SupplierItem.objects.create(supplier=self.supplier, brand="SACHS", sku="315 849", sku_norm="315849",
                                    cost=900, name="Амортизатор SACHS", available=True)
        PartFitment.objects.create(part_number=self.number, vehicle_model=self.model, source=self.source,
                                   year_from=2006, year_to=2014, verified=True)

    def test_car_parts_uses_structured_fitment_and_displays_evidence(self):
        response = self.client.get("/car-parts/?make=Renault&model=Trafic%20II&year=2010")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "315 849")
        self.assertContains(response, "Застосовність підтверджена")
        self.assertContains(response, "Fixture fitment source")

    def test_structured_fitment_respects_year_range(self):
        response = self.client.get("/car-parts/?make=Renault&model=Trafic%20II&year=2020")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "315 849")

    def test_cross_reference_screen_shows_real_stock_and_supplier_data(self):
        CrossRef.objects.create(a="315849", b="19152615", a_raw="315 849", b_raw="19-152615", source="manual")
        response = self.client.get("/crossrefs/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Підтверджено вручну")
        self.assertContains(response, "Амортизатор SACHS")
        self.assertContains(response, "Parts supplier")
