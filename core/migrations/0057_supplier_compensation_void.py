from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0056_supplier_compensations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="suppliercompensation",
            name="is_void",
            field=models.BooleanField(db_index=True, default=False, verbose_name="Скасовано"),
        ),
        migrations.AddField(
            model_name="suppliercompensation",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="suppliercompensation",
            name="void_reason",
            field=models.CharField(blank=True, max_length=250),
        ),
        migrations.AddField(
            model_name="suppliercompensation",
            name="voided_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="supplier_compensations_voided", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="suppliercompensation",
            name="reversal_cash_operation",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                       related_name="supplier_compensation_reversal", to="core.cashoperation"),
        ),
        migrations.AddField(
            model_name="suppliercompensation",
            name="reversal_bank_transaction",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                       related_name="supplier_compensation_reversal", to="core.banktransaction"),
        ),
    ]
