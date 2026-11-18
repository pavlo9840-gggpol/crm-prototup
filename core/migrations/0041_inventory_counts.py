from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0040_stock_serial_units"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryCount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("draft", "Чернетка"), ("approved", "Затверджено")], db_index=True, default="draft", max_length=12)),
                ("note", models.TextField(blank=True, verbose_name="Примітка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_inventory_counts", to=settings.AUTH_USER_MODEL)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_counts", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_inventory_counts", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="InventoryCountItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("expected_quantity", models.DecimalField(decimal_places=3, max_digits=14)),
                ("counted_quantity", models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
                ("counted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company")),
                ("inventory_count", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="core.inventorycount")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_count_items", to="core.product")),
            ],
            options={"ordering": ["product__name", "product__sku"]},
        ),
        migrations.AddConstraint(
            model_name="inventorycountitem",
            constraint=models.UniqueConstraint(fields=("inventory_count", "product"), name="uniq_inventory_count_product"),
        ),
        migrations.AddIndex(
            model_name="inventorycountitem",
            index=models.Index(fields=["company", "inventory_count"], name="inv_count_item_company_idx"),
        ),
    ]
