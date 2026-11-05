from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_product_condition_and_photo_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="salereturn",
            name="refund_method",
            field=models.CharField(
                choices=[
                    ("pending", "Очікує виплати"),
                    ("cash", "Готівка"),
                    ("card", "Картка"),
                    ("mixed", "Змішана"),
                ],
                default="pending",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="salereturn",
            name="cash_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="salereturn",
            name="card_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="salereturn",
            name="shift",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sale_returns",
                to="core.shift",
            ),
        ),
    ]
