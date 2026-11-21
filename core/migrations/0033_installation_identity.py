import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_service_payment_field_labels"),
    ]

    operations = [
        migrations.CreateModel(
            name="InstallationIdentity",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("installation_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="Код інсталяції")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Створено")),
            ],
            options={
                "verbose_name": "Ідентифікатор інсталяції",
                "verbose_name_plural": "Ідентифікатор інсталяції",
            },
        ),
    ]
