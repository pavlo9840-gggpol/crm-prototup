import datetime
from decimal import Decimal

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0061_bank_reconciliation_advances"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankaccount",
            name="opening_balance",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14, verbose_name="Початковий залишок"),
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="opening_balance_date",
            field=models.DateField(default=datetime.date(1900, 1, 1), verbose_name="Дата початкового залишку"),
        ),
        migrations.AlterField(
            model_name="bankaccount",
            name="opening_balance_date",
            field=models.DateField(default=django.utils.timezone.localdate, verbose_name="Дата початкового залишку"),
        ),
    ]
