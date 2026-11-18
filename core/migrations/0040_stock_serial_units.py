from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0039_stock_batches_fifo")]

    operations = [
        migrations.CreateModel(
            name="StockSerialUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("serial_number", models.CharField(max_length=100, verbose_name="Серійний номер / IMEI")),
                ("normalized_serial", models.CharField(editable=False, max_length=100)),
                ("status", models.CharField(choices=[("in_stock", "На складі"), ("sold", "Продано")], db_index=True, default="in_stock", max_length=12)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("sold_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_serial_units", to="core.company")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="serial_units", to="core.product")),
                ("purchase_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="serial_units", to="core.purchaseitem")),
                ("sale_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="serial_units", to="core.saleitem")),
            ],
            options={"ordering": ["product__name", "serial_number"]},
        ),
        migrations.AddConstraint(
            model_name="stockserialunit",
            constraint=models.UniqueConstraint(fields=("company", "normalized_serial"), name="uniq_company_serial_number"),
        ),
        migrations.AddIndex(
            model_name="stockserialunit",
            index=models.Index(fields=["company", "product", "status"], name="serial_stock_status_idx"),
        ),
    ]
