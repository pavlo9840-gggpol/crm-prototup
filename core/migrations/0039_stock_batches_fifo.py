# Generated for FIFO stock batches; existing stock remains compatible.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0038_universal_commerce_foundation")]

    operations = [
        migrations.CreateModel(
            name="StockBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("batch_number", models.CharField(blank=True, max_length=80, verbose_name="Номер партії")),
                ("expiry_date", models.DateField(blank=True, db_index=True, null=True, verbose_name="Термін придатності")),
                ("received_quantity", models.DecimalField(decimal_places=3, max_digits=12, verbose_name="Отримано")),
                ("remaining_quantity", models.DecimalField(decimal_places=3, max_digits=12, verbose_name="Залишок партії")),
                ("cost_price", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Собівартість одиниці")),
                ("received_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата надходження")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_batches", to="core.company")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_batches", to="core.product")),
                ("purchase_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="batches", to="core.purchaseitem")),
            ],
            options={"ordering": ["expiry_date", "received_at", "pk"]},
        ),
        migrations.AddIndex(model_name="stockbatch", index=models.Index(fields=["company", "product", "expiry_date"], name="batch_stock_exp_idx")),
        migrations.CreateModel(
            name="BatchAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=12)),
                ("returned_quantity", models.DecimalField(decimal_places=3, default=0, max_digits=12)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="batch_allocations", to="core.company")),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="allocations", to="core.stockbatch")),
                ("sale_item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="batch_allocations", to="core.saleitem")),
            ],
            options={"ordering": ["pk"]},
        ),
        migrations.AddConstraint(model_name="batchallocation", constraint=models.UniqueConstraint(fields=("sale_item", "batch"), name="uniq_sale_item_batch")),
    ]
