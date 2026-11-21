from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0050_bank_import_fingerprint_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BankReconciliation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("matched_at", models.DateTimeField(auto_now_add=True)),
                ("is_void", models.BooleanField(db_index=True, default=False)),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("void_reason", models.CharField(blank=True, max_length=250)),
                ("bank_transaction", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reconciliations", to="core.banktransaction")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bank_reconciliations", to="core.company")),
                ("matched_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bank_reconciliations_created", to=settings.AUTH_USER_MODEL)),
                ("settlement_payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bank_reconciliations", to="core.settlementpayment")),
                ("voided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="bank_reconciliations_voided", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-matched_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="bankreconciliation",
            constraint=models.UniqueConstraint(condition=models.Q(("is_void", False)), fields=("bank_transaction",), name="uniq_active_bank_reconciliation"),
        ),
        migrations.AddConstraint(
            model_name="bankreconciliation",
            constraint=models.UniqueConstraint(condition=models.Q(("is_void", False)), fields=("settlement_payment",), name="uniq_active_payment_reconciliation"),
        ),
    ]
