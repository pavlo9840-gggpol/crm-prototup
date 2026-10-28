from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import Customer, CustomerCar, Product
from core.service_models import ServiceAppointment, ServiceOrder, ServiceOrderLine


class ServiceWorkshopTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(username="workshop-admin", password="pass", is_staff=True)
        self.customer = Customer.objects.create(name="Олена", phone="+380501111111")
        self.car = CustomerCar.objects.create(customer=self.customer, make="Skoda", model="Octavia", year=2018, plate="AC1234BC", vin="TMBABCD1234567890")
        self.client.force_login(self.staff)

    def test_staff_can_create_appointment(self):
        response = self.client.post(reverse("service_appointment_new"), {
            "customer": self.customer.pk, "car": self.car.pk,
            "scheduled_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": 60, "complaint": "Сторонній звук", "assigned_to": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ServiceAppointment.objects.count(), 1)

    def test_order_totals_labor_and_parts(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Заміна масла", quantity=Decimal("1.5"), unit_price=Decimal("400"))
        product = Product.objects.create(name="Фільтр", sku="SVC-FILTER", price=Decimal("250"), cost_price=Decimal("100"))
        ServiceOrderLine.objects.create(order=order, kind="part", description="Фільтр", product=product, quantity=Decimal("2"), unit_price=Decimal("250"), unit_cost=product.cost_price)
        self.assertEqual(order.total, Decimal("1100.0"))
        self.assertEqual(product.current_stock, Decimal("0"))  # Drafting a repair order must not change stock.

    def test_non_staff_cannot_access_workshop(self):
        User = get_user_model()
        cashier = User.objects.create_user(username="workshop-cashier", password="pass", is_staff=False)
        self.client.force_login(cashier)
        response = self.client.get(reverse("service_home"))
        self.assertEqual(response.status_code, 403)
