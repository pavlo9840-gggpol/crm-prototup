# Link audited settlement payments to their cash drawer movements.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0045_sale_credit_payment_choice"),
    ]

    operations = [
        migrations.AddField(
            model_name="settlementpayment",
            name="cash_operation",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="settlement_payment", to="core.cashoperation",
            ),
        ),
        migrations.AddField(
            model_name="settlementpayment",
            name="reversal_operation",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="reversed_settlement_payment", to="core.cashoperation",
            ),
        ),
    ]
