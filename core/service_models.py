from django.db import models
from django.conf import settings

USER = settings.AUTH_USER_MODEL

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

    class Meta:
        ordering = ["scheduled_at"]
