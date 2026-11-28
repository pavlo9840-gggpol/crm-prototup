# Add the customer-credit option to POS payment choices.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_settlement_payments"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sale",
            name="payment_method",
            field=models.CharField(
                choices=[
                    ("cash", "Готівка"),
                    ("card", "Картка"),
                    ("mixed", "Змішана"),
                    ("credit", "У борг"),
                ],
                default="cash",
                max_length=10,
            ),
        ),
    ]
