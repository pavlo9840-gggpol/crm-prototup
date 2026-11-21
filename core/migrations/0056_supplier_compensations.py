from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0055_supplier_returns"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierCompensation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(blank=True, db_index=True, max_length=24, verbose_name="Номер компенсації")),
                ("kind", models.CharField(choices=[("credit", "Кредит-нота / залік боргу"), ("cash", "Повернення готівкою"), ("bank", "Повернення на банківський рахунок")], max_length=10, verbose_name="Тип компенсації")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Сума")),
                ("note", models.CharField(blank=True, max_length=250, verbose_name="Примітка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("bank_account", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_compensations", to="core.bankaccount")),
                ("bank_transaction", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_compensation", to="core.banktransaction")),
                ("cash_operation", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_compensation", to="core.cashoperation")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_compensations", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_compensations_created", to=settings.AUTH_USER_MODEL)),
                ("purchase", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_compensations", to="core.purchase")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="compensations", to="core.supplier")),
                ("supplier_return", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="compensations", to="core.supplierreturn")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddIndex(model_name="suppliercompensation", index=models.Index(fields=["company", "created_at"], name="sup_comp_company_date_idx")),
        migrations.AddIndex(model_name="suppliercompensation", index=models.Index(fields=["company", "supplier"], name="sup_comp_company_sup_idx")),
        migrations.AddConstraint(model_name="suppliercompensation", constraint=models.CheckConstraint(condition=Q(amount__gt=0), name="supplier_comp_amount_positive")),
    ]
