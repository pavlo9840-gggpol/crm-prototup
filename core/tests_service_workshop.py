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

    def test_overlapping_appointment_for_same_vehicle_is_rejected(self):
        start = timezone.now() + timedelta(days=2)
        ServiceAppointment.objects.create(
            customer=self.customer, car=self.car, scheduled_at=start,
            duration_minutes=90, complaint="Перша перевірка", created_by=self.staff,
        )
        response = self.client.post(reverse("service_appointment_new"), {
            "customer": self.customer.pk, "car": self.car.pk,
            "scheduled_at": (start + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": 60, "complaint": "Друга перевірка", "assigned_to": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ServiceAppointment.objects.count(), 1)
        self.assertContains(response, "Конфлікт запису")

    def test_overlapping_appointment_for_same_mechanic_is_rejected(self):
        mechanic = get_user_model().objects.create_user(username="mechanic", password="pass")
        other_customer = Customer.objects.create(name="Іван", phone="+380501111112")
        other_car = CustomerCar.objects.create(customer=other_customer, make="VW", model="Golf", year=2017, plate="AC9876BC", vin="WVWZZZ1JZXW000001")
        start = timezone.now() + timedelta(days=3)
        ServiceAppointment.objects.create(
            customer=self.customer, car=self.car, scheduled_at=start,
            duration_minutes=60, assigned_to=mechanic, created_by=self.staff,
        )
        response = self.client.post(reverse("service_appointment_new"), {
            "customer": other_customer.pk, "car": other_car.pk,
            "scheduled_at": (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": 60, "complaint": "Інший автомобіль", "assigned_to": mechanic.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ServiceAppointment.objects.count(), 1)
        self.assertContains(response, "Конфлікт запису")

    def test_order_totals_labor_and_parts(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Заміна масла", quantity=Decimal("1.5"), unit_price=Decimal("400"))
        product = Product.objects.create(name="Фільтр", sku="SVC-FILTER", price=Decimal("250"), cost_price=Decimal("100"))
        ServiceOrderLine.objects.create(order=order, kind="part", description="Фільтр", product=product, quantity=Decimal("2"), unit_price=Decimal("250"), unit_cost=product.cost_price)
        self.assertEqual(order.total, Decimal("1100.0"))
        self.assertEqual(product.current_stock, Decimal("0"))  # Drafting a repair order must not change stock.

    def test_issue_parts_is_atomic_and_cannot_be_repeated(self):
        from core.models import StockMovement
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="approved", created_by=self.staff)
        product = Product.objects.create(name="Гальмівні колодки", sku="SVC-PADS", price=Decimal("900"), cost_price=Decimal("500"), current_stock=Decimal("5"))
        line = ServiceOrderLine.objects.create(order=order, kind="part", description="Колодки", product=product, quantity=Decimal("2"), unit_price=Decimal("900"), unit_cost=Decimal("500"))
        response = self.client.post(reverse("service_order_issue_parts", args=[order.pk]))
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("3"))
        self.assertTrue(line.issued)
        self.assertEqual(StockMovement.objects.filter(doc_ref=f"STO-{order.pk:06d}").count(), 1)
        self.client.post(reverse("service_order_issue_parts", args=[order.pk]))
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("3"))
        self.assertEqual(StockMovement.objects.filter(doc_ref=f"STO-{order.pk:06d}").count(), 1)

    def test_issue_parts_shortage_does_not_partially_deduct_stock(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="in_progress", created_by=self.staff)
        first = Product.objects.create(name="Фільтр повітря", sku="SVC-AIR", current_stock=Decimal("5"))
        second = Product.objects.create(name="Свічка", sku="SVC-PLUG", current_stock=Decimal("1"))
        l1 = ServiceOrderLine.objects.create(order=order, kind="part", description="Фільтр", product=first, quantity=Decimal("2"), unit_price=Decimal("100"))
        l2 = ServiceOrderLine.objects.create(order=order, kind="part", description="Свічки", product=second, quantity=Decimal("2"), unit_price=Decimal("50"))
        self.client.post(reverse("service_order_issue_parts", args=[order.pk]))
        first.refresh_from_db()
        second.refresh_from_db()
        l1.refresh_from_db()
        l2.refresh_from_db()
        self.assertEqual(first.current_stock, Decimal("5"))
        self.assertEqual(second.current_stock, Decimal("1"))
        self.assertFalse(l1.issued)
        self.assertFalse(l2.issued)

    def test_vehicle_history_is_shown_on_order_detail(self):
        older = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="delivered", created_by=self.staff)
        ServiceOrderLine.objects.create(order=older, kind="labor", description="Заміна гальмівної рідини", unit_price=Decimal("600"))
        current = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        response = self.client.get(reverse("service_order_detail", args=[current.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заміна гальмівної рідини")
        self.assertContains(response, f"#{older.pk}")

    def test_cannot_add_order_lines_after_approval(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="in_progress", created_by=self.staff)
        response = self.client.post(reverse("service_order_detail", args=[order.pk]), {
            "add_line": "1", "kind": "labor", "description": "Заміна", "quantity": "1", "unit_price": "100", "product": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order.lines.count(), 0)


    def test_status_menu_only_shows_allowed_transitions(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="approval", created_by=self.staff)
        response = self.client.get(reverse("service_order_detail", args=[order.pk]))
        self.assertContains(response, '<option value="approved">')
        self.assertContains(response, '<option value="cancelled">')
        self.assertNotContains(response, '<option value="delivered">')

    def test_order_creation_form_does_not_allow_manual_payment_edits(self):
        response = self.client.get(reverse("service_order_new"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="paid_amount"')
        self.assertNotContains(response, 'name="payment_note"')


    def test_card_payment_updates_balance_and_is_audited(self):
        from core.service_models import ServicePayment
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Діагностика", quantity=1, unit_price=Decimal("500"))
        response = self.client.post(reverse("service_order_payment", args=[order.pk]), {
            "amount": "200.00", "method": "card", "note": "Передоплата",
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, Decimal("200.00"))
        self.assertEqual(order.balance_due, Decimal("300.00"))
        payment = ServicePayment.objects.get(order=order)
        self.assertEqual(payment.method, "card")
        self.assertIsNone(payment.shift)

    def test_cash_payment_updates_drawer_and_cash_operation(self):
        from core.models import CashOperation, Shift
        from core.service_models import ServicePayment
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Діагностика", quantity=1, unit_price=Decimal("350"))
        shift = Shift.objects.create(cashier=self.staff, drawer={"10000": 1})
        response = self.client.post(reverse("service_order_payment", args=[order.pk]), {
            "amount": "150.00", "method": "cash", "note": "Готівкова передоплата",
        })
        self.assertEqual(response.status_code, 302)
        shift.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, Decimal("150.00"))
        self.assertEqual(shift.drawer.get("10000"), 2)
        self.assertEqual(shift.drawer.get("5000"), 1)
        operation = CashOperation.objects.get(shift=shift, note__contains="СТО")
        self.assertEqual(operation.kind, "in")
        self.assertEqual(operation.amount, Decimal("150.00"))
        self.assertEqual(ServicePayment.objects.get(order=order).shift, shift)

    def test_cash_payment_requires_open_shift(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Діагностика", unit_price=Decimal("100"))
        response = self.client.post(reverse("service_order_payment", args=[order.pk]), {
            "amount": "50", "method": "cash",
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, Decimal("0"))
        self.assertEqual(order.payments.count(), 0)

    def test_vehicle_cannot_be_delivered_with_outstanding_balance(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="completed", created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Ремонт", unit_price=Decimal("500"))
        self.client.post(reverse("service_order_status", args=[order.pk]), {"status": "delivered"})
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")

    def test_repair_cannot_be_completed_before_parts_are_issued(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, status="in_progress", created_by=self.staff)
        product = Product.objects.create(name="Фільтр", sku="SVC-ISSUE-REQUIRED", current_stock=Decimal("2"))
        ServiceOrderLine.objects.create(order=order, kind="part", description="Фільтр", product=product, quantity=1, unit_price=Decimal("100"))
        self.client.post(reverse("service_order_status", args=[order.pk]), {"status": "completed"})
        order.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(order.status, "in_progress")
        self.assertEqual(product.current_stock, Decimal("2"))

    def test_payment_cannot_exceed_balance(self):
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Діагностика", unit_price=Decimal("100"))
        self.client.post(reverse("service_order_payment", args=[order.pk]), {
            "amount": "100.01", "method": "card",
        })
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, Decimal("0"))
        self.assertEqual(order.payments.count(), 0)

    def test_workshop_reports_show_monthly_labor_parts_and_payments(self):
        from core.service_models import ServicePayment
        order = ServiceOrder.objects.create(customer=self.customer, car=self.car, created_by=self.staff)
        ServiceOrderLine.objects.create(order=order, kind="labor", description="Діагностика", quantity=1, unit_price=Decimal("400"))
        product = Product.objects.create(name="Фільтр", sku="SVC-REPORT-FILTER")
        ServiceOrderLine.objects.create(order=order, kind="part", description="Фільтр", product=product, quantity=2, unit_price=Decimal("150"))
        ServicePayment.objects.create(order=order, amount=Decimal("200"), method="card", received_by=self.staff)
        response = self.client.get(reverse("service_reports"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "400.00 грн")
        self.assertContains(response, "300.00 грн")
        self.assertContains(response, "200.00 грн")
        self.assertContains(response, f"#{order.pk}")

    def test_non_staff_cannot_access_workshop(self):
        User = get_user_model()
        cashier = User.objects.create_user(username="workshop-cashier", password="pass", is_staff=False)
        self.client.force_login(cashier)
        response = self.client.get(reverse("service_home"))
        self.assertEqual(response.status_code, 403)
