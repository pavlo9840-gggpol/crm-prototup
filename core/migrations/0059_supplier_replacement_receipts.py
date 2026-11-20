from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0058_supplier_credit_applications"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierReplacementReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=12, verbose_name="Кількість заміни")),
                ("unit_cost", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Облікова собівартість")),
                ("note", models.CharField(blank=True, max_length=250)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_replacement_receipts", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_replacement_receipts_created", to=settings.AUTH_USER_MODEL)),
                ("purchase", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_replacement_receipts", to="core.purchase")),
                ("purchase_item", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_replacement_receipt", to="core.purchaseitem")),
                ("supplier_return", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="replacement_receipts", to="core.supplierreturn")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddConstraint(model_name="supplierreplacementreceipt", constraint=models.CheckConstraint(condition=models.Q(("quantity__gt", 0)), name="supplier_replacement_qty_positive")),
        migrations.AddConstraint(model_name="supplierreplacementreceipt", constraint=models.CheckConstraint(condition=models.Q(("unit_cost__gte", 0)), name="supplier_replacement_cost_nonnegative")),
    ]
