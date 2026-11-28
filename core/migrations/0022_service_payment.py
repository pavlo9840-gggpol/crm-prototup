from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_service_workshop"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ServicePayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Сума оплати, грн")),
                ("method", models.CharField(choices=[("cash", "Готівка"), ("card", "Картка")], max_length=10)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="core.serviceorder")),
                ("received_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="service_payments", to=settings.AUTH_USER_MODEL)),
                ("shift", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="service_payments", to="core.shift")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
    ]
