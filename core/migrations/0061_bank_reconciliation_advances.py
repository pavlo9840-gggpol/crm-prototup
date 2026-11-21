from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0060_alter_stockmovement_movement_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bankreconciliation",
            name="settlement_payment",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="bank_reconciliations", to="core.settlementpayment",
            ),
        ),
        migrations.AddField(
            model_name="bankreconciliation",
            name="account_advance",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="bank_reconciliations", to="core.accountadvance",
            ),
        ),
        migrations.AddConstraint(
            model_name="bankreconciliation",
            constraint=models.UniqueConstraint(
                condition=Q(("account_advance__isnull", False), ("is_void", False)),
                fields=("account_advance",), name="uniq_active_advance_reconciliation",
            ),
        ),
        migrations.AddConstraint(
            model_name="bankreconciliation",
            constraint=models.CheckConstraint(
                condition=(Q(("account_advance__isnull", True), ("settlement_payment__isnull", False)) |
                           Q(("account_advance__isnull", False), ("settlement_payment__isnull", True))),
                name="bank_reconciliation_exactly_one_source",
            ),
        ),
    ]
