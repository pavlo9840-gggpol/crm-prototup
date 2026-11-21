from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_installation_identity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LicenseWorkstationSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_key", models.CharField(max_length=40, unique=True, verbose_name="Ключ сесії")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Початок сесії")),
                ("last_seen", models.DateTimeField(db_index=True, verbose_name="Остання активність")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="license_workstation_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Активне робоче місце",
                "verbose_name_plural": "Активні робочі місця",
                "ordering": ["last_seen"],
            },
        ),
    ]
