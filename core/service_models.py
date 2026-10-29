from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

USER = settings.AUTH_USER_MODEL
D0 = Decimal("0")


class ServiceAppointment(models.Model):
    STATUS = [("booked", "Заплановано"), ("arrived", "Клієнт прибув"), ("converted", "Створено наряд"), ("cancelled", "Скасовано")]
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="service_appointments")
    car = models.ForeignKey("core.CustomerCar", on_delete=models.PROTECT, related_name="service_appointments")
    scheduled_at = models.DateTimeField("Дата й час запису", db_index=True)
    duration_minutes = models.PositiveSmallIntegerField("Тривалість, хв", default=60)
    complaint = models.TextField("Скарга клієнта", blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="booked", db_index=True)
    assigned_to = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="service_appointments")
    created_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="created_service_appointments")
    created_at = models.DateTimeField(auto_now_add=True)
    def clean(self):
        if self.car_id and self.customer_id and self.car.customer_id != self.customer_id:
            raise ValidationError({"car": "Автомобіль має належати вибраному клієнту."})
        if not 15 <= self.duration_minutes <= 720:
            raise ValidationError({"duration_minutes": "Тривалість має бути від 15 до 720 хвилин."})
    class Meta:
        ordering = ["scheduled_at"]
        indexes = [models.Index(fields=["scheduled_at", "status"], name="svc_appt_date_status_idx")]


class ServiceOrder(models.Model):
    STATUS = [("draft", "Чернетка"), ("diagnosis", "Діагностика"), ("approval", "Очікує погодження"),
              ("approved", "Погоджено"), ("in_progress", "У роботі"), ("waiting_parts", "Очікує запчастини"),
              ("completed", "Ремонт завершено"), ("delivered", "Видано клієнту"), ("cancelled", "Скасовано")]
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="service_orders")
    car = models.ForeignKey("core.CustomerCar", on_delete=models.PROTECT, related_name="service_orders")
    appointment = models.OneToOneField(ServiceAppointment, null=True, blank=True, on_delete=models.SET_NULL, related_name="service_order")
    mechanic = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="service_orders")
    status = models.CharField(max_length=20, choices=STATUS, default="draft", db_index=True)
    mileage = models.PositiveIntegerField("Пробіг, км", null=True, blank=True)
    complaint = models.TextField("Скарга клієнта", blank=True)
    diagnosis = models.TextField("Діагностика", blank=True)
    recommendation = models.TextField("Рекомендації", blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    paid_amount = models.DecimalField("Сплачено, грн", max_digits=12, decimal_places=2, default=0)
    payment_note = models.CharField("Примітка до оплати", max_length=250, blank=True)
    created_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="created_service_orders")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), D0)
    @property
    def balance_due(self):
        return max(D0, self.total - self.paid_amount)
    def clean(self):
        if self.car_id and self.customer_id and self.car.customer_id != self.customer_id:
            raise ValidationError({"car": "Автомобіль має належати вибраному клієнту."})
        if self.paid_amount < 0:
            raise ValidationError({"paid_amount": "Оплата не може бути від'ємною."})
    class Meta:
        ordering = ["-created_at"]


class ServiceOrderLine(models.Model):
    LABOR, PART = "labor", "part"
    KINDS = [(LABOR, "Робота"), (PART, "Запчастина")]
    order = models.ForeignKey(ServiceOrder, on_delete=models.CASCADE, related_name="lines")
    kind = models.CharField(max_length=10, choices=KINDS)
    description = models.CharField("Робота / запчастина", max_length=200)
    product = models.ForeignKey("core.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="service_order_lines")
    quantity = models.DecimalField("Кількість", max_digits=10, decimal_places=3, default=1)
    unit_price = models.DecimalField("Ціна за одиницю", max_digits=12, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    issued = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    @property
    def total(self):
        return self.quantity * self.unit_price
    def clean(self):
        if self.quantity <= 0:
            raise ValidationError({"quantity": "Кількість має бути більшою за нуль."})
        if self.unit_price < 0 or self.unit_cost < 0:
            raise ValidationError("Ціни не можуть бути від'ємними.")
        if self.kind == self.PART and not self.product_id:
            raise ValidationError({"product": "Для запчастини потрібно вибрати товар зі складу."})
        if self.kind == self.LABOR and self.product_id:
            raise ValidationError({"product": "Робота не повинна списувати товар зі складу."})
    class Meta:
        ordering = ["id"]


class ServicePayment(models.Model):
    METHODS = [("cash", "Готівка"), ("card", "Картка")]
    order = models.ForeignKey(ServiceOrder, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField("Сума оплати, грн", max_digits=12, decimal_places=2)
    method = models.CharField("Спосіб оплати", max_length=10, choices=METHODS)
    note = models.CharField("Примітка", max_length=200, blank=True)
    received_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="service_payments")
    shift = models.ForeignKey("core.Shift", null=True, blank=True, on_delete=models.PROTECT, related_name="service_payments")
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.amount <= D0:
            raise ValidationError({"amount": "Сума оплати має бути більшою за нуль."})

    class Meta:
        ordering = ["-created_at", "-pk"]
