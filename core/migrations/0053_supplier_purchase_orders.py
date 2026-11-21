from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_stock_adjustment_reasons"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(blank=True, db_index=True, max_length=24, verbose_name="Номер замовлення")),
                ("status", models.CharField(choices=[("draft", "Чернетка"), ("sent", "Відправлено"), ("confirmed", "Підтверджено"), ("partial", "Частково отримано"), ("received", "Отримано"), ("cancelled", "Скасовано")], db_index=True, default="draft", max_length=12, verbose_name="Статус")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("expected_date", models.DateField(blank=True, null=True, verbose_name="Очікувана дата")),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.TextField(blank=True, verbose_name="Примітка")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_orders", to="core.company")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_orders", to="core.supplier")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_purchase_orders", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="PurchaseOrderItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=12, verbose_name="Замовлено")),
                ("received_quantity", models.DecimalField(decimal_places=3, default=0, max_digits=12, verbose_name="Отримано")),
                ("cost_price", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Ціна замовлення")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_order_items", to="core.company")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="core.purchaseorder")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_order_items", to="core.product")),
                ("supplier_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="purchase_order_items", to="core.supplieritem")),
            ],
            options={"ordering": ["product__name", "pk"]},
        ),
        migrations.AddField(
            model_name="purchaseitem",
            name="order_item",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="receipt_items", to="core.purchaseorderitem"),
        ),
        migrations.AddIndex(
            model_name="purchaseorder",
            index=models.Index(fields=["company", "status", "created_at"], name="po_company_status_idx"),
        ),
        migrations.AddIndex(
            model_name="purchaseorder",
            index=models.Index(fields=["company", "supplier", "status"], name="po_company_supplier_idx"),
        ),
        migrations.AddIndex(
            model_name="purchaseorderitem",
            index=models.Index(fields=["company", "product"], name="poi_company_product_idx"),
        ),
    ]
