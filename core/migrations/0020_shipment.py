from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_catalog"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Shipment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("recipient_name", models.CharField(max_length=150, verbose_name="Одержувач")),
                ("recipient_phone", models.CharField(max_length=30, verbose_name="Телефон одержувача")),
                ("recipient_city", models.CharField(blank=True, max_length=150, verbose_name="Місто одержувача")),
                ("recipient_city_ref", models.CharField(blank=True, max_length=80)),
                ("recipient_warehouse", models.CharField(blank=True, max_length=250, verbose_name="Відділення / адреса")),
                ("recipient_warehouse_ref", models.CharField(blank=True, max_length=80)),
                ("ttn", models.CharField(blank=True, max_length=40, null=True, unique=True, verbose_name="ТТН")),
                ("description", models.CharField(default="Автозапчастини", max_length=200, verbose_name="Опис відправлення")),
                ("weight", models.DecimalField(decimal_places=3, default=1, max_digits=8, verbose_name="Вага, кг")),
                ("seats_amount", models.PositiveSmallIntegerField(default=1, verbose_name="Кількість місць")),
                ("declared_cost", models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name="Оголошена вартість, грн")),
                ("cod_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name="Післяплата, грн")),
                ("status", models.CharField(choices=[("draft", "Чернетка"), ("created", "Накладну створено"), ("in_transit", "У дорозі"), ("arrived", "Прибуло у відділення"), ("delivered", "Отримано"), ("returned", "Повернення"), ("problem", "Потрібна увага")], db_index=True, default="draft", max_length=20)),
                ("status_text", models.CharField(blank=True, max_length=250, verbose_name="Статус перевізника")),
                ("last_tracked_at", models.DateTimeField(blank=True, null=True)),
                ("api_error", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_shipments", to=settings.AUTH_USER_MODEL)),
                ("customer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="shipments", to="core.customer", verbose_name="Клієнт")),
                ("sale", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="shipments", to="core.sale", verbose_name="Продаж")),
            ],
            options={"verbose_name": "Відправлення", "verbose_name_plural": "Відправлення", "ordering": ["-created_at"]},
        ),
    ]
