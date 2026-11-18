from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0047_bank_transactions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AccountAdvance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("party_type", models.CharField(choices=[("customer", "Аванс клієнта"), ("supplier", "Аванс постачальнику")], db_index=True, max_length=10)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Початкова сума")),
                ("remaining", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Невикористаний залишок")),
                ("payment_method", models.CharField(choices=[("cash", "Готівка"), ("card", "Картка"), ("bank", "Банк")], max_length=10)),
                ("document_ref", models.CharField(blank=True, max_length=80)),
                ("note", models.CharField(blank=True, max_length=250)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("is_void", models.BooleanField(db_index=True, default=False)),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("void_reason", models.CharField(blank=True, max_length=250)),
                ("cash_operation", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="account_advance", to="core.cashoperation")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="account_advances", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="account_advances", to=settings.AUTH_USER_MODEL)),
                ("customer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="account_advances", to="core.customer")),
                ("supplier", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="account_advances", to="core.supplier")),
                ("voided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="voided_account_advances", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(model_name="accountadvance", index=models.Index(fields=["company", "party_type", "created_at"], name="advance_company_type_date_idx")),
        migrations.AddConstraint(model_name="accountadvance", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="account_advance_amount_positive")),
        migrations.AddConstraint(model_name="accountadvance", constraint=models.CheckConstraint(condition=models.Q(("remaining__gte", 0)), name="account_advance_remaining_nonnegative")),
        migrations.AddConstraint(model_name="accountadvance", constraint=models.CheckConstraint(condition=(models.Q(("customer__isnull", False), ("party_type", "customer"), ("supplier__isnull", True)) | models.Q(("customer__isnull", True), ("party_type", "supplier"), ("supplier__isnull", False))), name="account_advance_exactly_one_party")),
        migrations.AddField(
            model_name="settlementpayment", name="advance",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="applications", to="core.accountadvance"),
        ),
        migrations.AlterField(
            model_name="settlementpayment", name="payment_method",
            field=models.CharField(choices=[("cash", "Готівка"), ("card", "Картка"), ("bank", "Банківський рахунок"), ("advance", "З авансу")], default="cash", max_length=10, verbose_name="Спосіб оплати"),
        ),
    ]
