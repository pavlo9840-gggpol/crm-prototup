from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0054_supplier_claims"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierReturn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(blank=True, db_index=True, max_length=24, verbose_name="Номер повернення")),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=12, verbose_name="Кількість")),
                ("unit_cost", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Собівартість одиниці")),
                ("compensation_type", models.CharField(choices=[("credit", "Зарахувати в рахунок боргу"), ("refund", "Повернення коштів"), ("replacement", "Заміна товару")], default="credit", max_length=12, verbose_name="Очікувана компенсація")),
                ("reason", models.TextField(verbose_name="Причина повернення")),
                ("note", models.TextField(blank=True, verbose_name="Примітка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField()),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_returns", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_returns_created", to=settings.AUTH_USER_MODEL)),
                ("purchase_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_returns", to="core.purchaseitem")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="returns", to="core.supplier")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddIndex(model_name="supplierreturn", index=models.Index(fields=["company", "created_at"], name="sup_return_company_date_idx")),
        migrations.AddIndex(model_name="supplierreturn", index=models.Index(fields=["company", "supplier"], name="sup_return_company_sup_idx")),
        migrations.AddConstraint(model_name="supplierreturn", constraint=models.CheckConstraint(condition=Q(quantity__gt=0), name="supplier_return_qty_positive")),
        migrations.AddConstraint(model_name="supplierreturn", constraint=models.CheckConstraint(condition=Q(unit_cost__gte=0), name="supplier_return_cost_nonnegative")),
    ]
