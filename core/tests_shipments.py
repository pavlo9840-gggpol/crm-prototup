from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Customer, Shipment


class ShipmentWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(username="shipping-staff", password="test-pass", is_staff=True)
        self.cashier = User.objects.create_user(username="shipping-cashier", password="test-pass", is_staff=False)

    def test_staff_can_create_draft_and_search_it(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("shipment_new"), {
            "recipient_name": "Олена Коваль", "recipient_phone": "+380501112233",
            "recipient_city": "Луцьк", "recipient_city_ref": "CITY-REF",
            "recipient_warehouse": "Відділення №1", "recipient_warehouse_ref": "WAREHOUSE-REF",
            "description": "Фільтр оливи", "weight": "1.250", "seats_amount": "1",
            "declared_cost": "1500", "cod_amount": "0",
        })
        self.assertRedirects(response, reverse("shipment_list"))
        shipment = Shipment.objects.get()
        self.assertEqual(shipment.status, "draft")
        self.assertEqual(shipment.recipient_name, "Олена Коваль")
        response = self.client.get(reverse("shipment_list"), {"q": "Олена"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Олена Коваль")

    def test_invalid_weight_does_not_create_shipment(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("shipment_new"), {
            "recipient_name": "Олена Коваль", "recipient_phone": "0501112233",
            "weight": "-1", "seats_amount": "1", "declared_cost": "0", "cod_amount": "0",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Shipment.objects.count(), 0)

    def test_cashier_cannot_access_shipment_workflow(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("shipment_list")).status_code, 403)
        response = self.client.post(reverse("shipment_new"), {
            "recipient_name": "No", "recipient_phone": "0500000000",
            "weight": "1", "seats_amount": "1", "declared_cost": "0", "cod_amount": "0",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Shipment.objects.count(), 0)

    def test_ttn_creation_requires_server_configuration_and_does_not_fake_success(self):
        from unittest.mock import patch
        self.client.force_login(self.staff)
        shipment = Shipment.objects.create(
            recipient_name="Олена Коваль", recipient_phone="0501112233",
            recipient_city_ref="CITY-REF", recipient_warehouse_ref="WAREHOUSE-REF",
            created_by=self.staff,
        )
        with patch.dict("os.environ", {"NOVA_POSHTA_API_KEY": ""}):
            response = self.client.post(reverse("shipment_create_ttn", args=[shipment.pk]))
        self.assertRedirects(response, reverse("shipment_list"))
        shipment.refresh_from_db()
        self.assertFalse(shipment.ttn)
        self.assertEqual(shipment.status, "draft")
        self.assertTrue(shipment.api_error)
