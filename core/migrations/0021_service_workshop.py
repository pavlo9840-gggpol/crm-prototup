from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0020_shipment"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="ServiceAppointment", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("scheduled_at", models.DateTimeField(db_index=True, verbose_name="Дата й час запису")),
            ("duration_minutes", models.PositiveSmallIntegerField(default=60, verbose_name="Тривалість, хв")),
            ("complaint", models.TextField(blank=True, verbose_name="Скарга клієнта")),
            ("status", models.CharField(choices=[("booked", "Заплановано"), ("arrived", "Клієнт прибув"), ("converted", "Створено наряд"), ("cancelled", "Скасовано")], db_index=True, default="booked", max_length=20)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="service_appointments", to=settings.AUTH_USER_MODEL)),
            ("car", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_appointments", to="core.customercar")),
            ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_service_appointments", to=settings.AUTH_USER_MODEL)),
            ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_appointments", to="core.customer")),
        ], options={"ordering": ["scheduled_at"], "indexes": [models.Index(fields=["scheduled_at", "status"], name="svc_appt_date_status_idx")]}),
        migrations.CreateModel(name="ServiceOrder", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("status", models.CharField(choices=[("draft", "Чернетка"), ("diagnosis", "Діагностика"), ("approval", "Очікує погодження"), ("approved", "Погоджено"), ("in_progress", "У роботі"), ("waiting_parts", "Очікує запчастини"), ("completed", "Ремонт завершено"), ("delivered", "Видано клієнту"), ("cancelled", "Скасовано")], db_index=True, default="draft", max_length=20)),
            ("mileage", models.PositiveIntegerField(blank=True, null=True, verbose_name="Пробіг, км")),
            ("complaint", models.TextField(blank=True, verbose_name="Скарга клієнта")),
            ("diagnosis", models.TextField(blank=True, verbose_name="Діагностика")),
            ("recommendation", models.TextField(blank=True, verbose_name="Рекомендації")),
            ("approved_at", models.DateTimeField(blank=True, null=True)),
            ("completed_at", models.DateTimeField(blank=True, null=True)),
            ("delivered_at", models.DateTimeField(blank=True, null=True)),
            ("paid_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name="Сплачено, грн")),
            ("payment_note", models.CharField(blank=True, max_length=250, verbose_name="Примітка до оплати")),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("appointment", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="service_order", to="core.serviceappointment")),
            ("car", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_orders", to="core.customercar")),
            ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_service_orders", to=settings.AUTH_USER_MODEL)),
            ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_orders", to="core.customer")),
            ("mechanic", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="service_orders", to=settings.AUTH_USER_MODEL)),
        ], options={"ordering": ["-created_at"]}),
        migrations.CreateModel(name="ServiceOrderLine", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("kind", models.CharField(choices=[("labor", "Робота"), ("part", "Запчастина")], max_length=10)),
            ("description", models.CharField(max_length=200, verbose_name="Робота / запчастина")),
            ("quantity", models.DecimalField(decimal_places=3, default=1, max_digits=10, verbose_name="Кількість")),
            ("unit_price", models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name="Ціна за одиницю")),
            ("unit_cost", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
            ("issued", models.BooleanField(db_index=True, default=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="core.serviceorder")),
            ("product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="service_order_lines", to="core.product")),
        ], options={"ordering": ["id"]}),
    ]
