# Generated for the universal commerce foundation; existing product data is preserved.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0037_remove_legacy_company_membership")]

    operations = [
        migrations.AddField(model_name="company", name="business_type", field=models.CharField(
            choices=[("general_retail", "Роздрібна торгівля"), ("grocery", "Продуктовий магазин"),
                     ("auto_parts", "Автозапчастини"), ("electronics", "Електроніка та техніка"),
                     ("online_store", "Інтернет-магазин")],
            default="auto_parts", max_length=24, verbose_name="Профіль бізнесу")),
        migrations.AddField(model_name="company", name="enabled_modules",
                            field=models.JSONField(blank=True, default=list, verbose_name="Увімкнені модулі")),
        migrations.AddField(model_name="product", name="product_kind", field=models.CharField(
            choices=[("goods", "Товар"), ("service", "Послуга")], db_index=True, default="goods",
            max_length=12, verbose_name="Тип позиції")),
        migrations.AddField(model_name="product", name="tracking_mode", field=models.CharField(
            choices=[("none", "Без додаткового обліку"), ("batch", "Партії"), ("expiry", "Партії та термін придатності"),
                     ("serial", "Серійні номери")], default="none", max_length=12, verbose_name="Спосіб обліку")),
        migrations.AddField(model_name="product", name="tax_category", field=models.CharField(
            choices=[("general", "Загальна категорія"), ("vat_standard", "ПДВ — основна ставка"),
                     ("vat_reduced", "ПДВ — знижена ставка"), ("vat_exempt", "Без ПДВ / звільнено"),
                     ("excise", "Підакцизний товар")], default="general", max_length=20, verbose_name="Податкова категорія")),
        migrations.AddField(model_name="product", name="weight_kg", field=models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name="Вага, кг")),
        migrations.AddField(model_name="product", name="length_cm", field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name="Довжина, см")),
        migrations.AddField(model_name="product", name="width_cm", field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name="Ширина, см")),
        migrations.AddField(model_name="product", name="height_cm", field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name="Висота, см")),
        migrations.AddField(model_name="product", name="attributes", field=models.JSONField(blank=True, default=dict, verbose_name="Додаткові характеристики")),
        migrations.CreateModel(name="ProductBarcode", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("barcode", models.CharField(max_length=80, verbose_name="Штрих-код")),
            ("label", models.CharField(blank=True, max_length=80, verbose_name="Позначення")),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="product_barcodes", to="core.company")),
            ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="extra_barcodes", to="core.product")),
        ], options={"verbose_name": "Додатковий штрих-код", "verbose_name_plural": "Додаткові штрих-коди", "ordering": ["barcode"]}),
        migrations.AddConstraint(model_name="productbarcode", constraint=models.UniqueConstraint(fields=("company", "barcode"), name="uniq_company_extra_barcode")),
        migrations.AddIndex(model_name="productbarcode", index=models.Index(fields=["product", "barcode"], name="pbarcode_prod_idx")),
    ]
