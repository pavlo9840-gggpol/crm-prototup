import csv
import io
import json
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from core.models import (CatalogPart, CatalogPartNumber, CatalogSource, PartFitment,
                         PartOe, VehicleMake, VehicleModel)


class VehicleCatalogueImportTests(TestCase):
    @patch("core.management.commands.import_vehiclesdb.urlopen")
    def test_vehiclesdb_import_is_idempotent(self, mock_urlopen):
        payload = {
            "version": "test-1",
            "makes": [{
                "name": "Example Motors", "slug": "example-motors",
                "models": [
                    {"name": "Roadster", "slug": "roadster", "kind": "car", "id": "car/example-motors/roadster"},
                    {"name": "Hauler", "slug": "hauler", "kind": "truck", "id": "truck/example-motors/hauler"},
                ],
            }],
        }
        class Response:
            def __enter__(self):
                return io.BytesIO(json.dumps(payload).encode("utf-8"))
            def __exit__(self, *args):
                return False
        mock_urlopen.return_value = Response()

        call_command("import_vehiclesdb", stdout=io.StringIO())
        call_command("import_vehiclesdb", stdout=io.StringIO())

        self.assertEqual(VehicleMake.objects.count(), 2)
        self.assertEqual(VehicleModel.objects.count(), 2)
        self.assertEqual(VehicleModel.objects.filter(slug="roadster").count(), 1)
        self.assertEqual(CatalogSource.objects.filter(name="VehiclesDB open dataset").count(), 1)

    def test_parts_csv_import_does_not_duplicate_parts_oe_or_fitments(self):
        source = CatalogSource.objects.create(name="Test licensed data", license_name="Test license")
        make = VehicleMake.objects.create(name="Example Motors", slug="example-motors", kind="car",
                                          source=source, external_id="car/example-motors", name_norm="example motors")
        model = VehicleModel.objects.create(make=make, name="Roadster", slug="roadster",
                                            source_id="car/example-motors/roadster")
        content = (
            "brand,sku,name,category,oe,make_slug,model_slug,kind,year_from,year_to,engine_code,verified\n"
            "ExampleBrand,AB-123,Oil filter,Filters,OE-123,example-motors,roadster,car,2010,2015,E1,true\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", suffix=".csv") as f:
            f.write(content)
            f.flush()
            for _ in range(2):
                call_command("import_parts_catalog", f.name, source="Test licensed data", stdout=io.StringIO())

        self.assertEqual(CatalogPartNumber.objects.count(), 1)
        self.assertEqual(CatalogPart.objects.count(), 1)
        self.assertEqual(PartOe.objects.count(), 1)
        self.assertEqual(PartFitment.objects.count(), 1)
        self.assertEqual(PartFitment.objects.first().vehicle_model_id, model.pk)


    def test_catalogue_search_includes_existing_manual_products(self):
        from django.contrib.auth import get_user_model
        from core.models import Product
        user = get_user_model().objects.create_user(username="catalog-user", password="test-pass")
        Product.objects.create(name="Oil filter", sku="AB-123", brand="ExampleBrand", price=25)
        self.client.login(username="catalog-user", password="test-pass")
        response = self.client.get("/catalogue/?q=AB123")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AB-123")
        self.assertContains(response, "Oil filter")
