# Generated for audited customer and supplier settlement payments.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0043_finance_entry_void_audit"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SettlementPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Сума")),
                ("payment_method", models.CharField(choices=[("cash", "Готівка"), ("card", "Картка"), ("bank", "Банківський рахунок")], default="cash", max_length=10, verbose_name="Спосіб оплати")),
                ("document_ref", models.CharField(blank=True, max_length=80, verbose_name="Номер документа оплати")),
                ("note", models.CharField(blank=True, max_length=250, verbose_name="Примітка")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата оплати")),
                ("is_void", models.BooleanField(db_index=True, default=False, verbose_name="Скасовано")),
                ("voided_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата скасування")),
                ("void_reason", models.CharField(blank=True, max_length=250, verbose_name="Причина скасування")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="settlement_payments", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="settlement_payments", to=settings.AUTH_USER_MODEL)),
                ("voided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="voided_settlement_payments", to=settings.AUTH_USER_MODEL)),
                ("purchase", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="settlement_payments", to="core.purchase")),
                ("sale", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="settlement_payments", to="core.sale")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(model_name="settlementpayment", index=models.Index(fields=["company", "created_at"], name="settlement_company_date_idx")),
        migrations.AddIndex(model_name="settlementpayment", index=models.Index(fields=["company", "is_void"], name="settlement_company_void_idx")),
        migrations.AddConstraint(model_name="settlementpayment", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="settlement_payment_amount_positive")),
        migrations.AddConstraint(model_name="settlementpayment", constraint=models.CheckConstraint(condition=(models.Q(("purchase__isnull", True), ("sale__isnull", False)) | models.Q(("purchase__isnull", False), ("sale__isnull", True))), name="settlement_exactly_one_document")),
    ]
