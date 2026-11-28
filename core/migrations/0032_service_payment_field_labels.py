from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_local_staff_role_labels"),
    ]

    operations = [
        migrations.AlterField(
            model_name="servicepayment",
            name="method",
            field=models.CharField(
                choices=[("cash", "Готівка"), ("card", "Картка")],
                max_length=10,
                verbose_name="Спосіб оплати",
            ),
        ),
        migrations.AlterField(
            model_name="servicepayment",
            name="note",
            field=models.CharField(
                blank=True,
                max_length=200,
                verbose_name="Примітка",
            ),
        ),
    ]
