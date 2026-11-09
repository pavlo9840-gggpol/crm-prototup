from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_unique_company_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="phone",
            field=models.CharField(max_length=30, verbose_name="Телефон"),
        ),
        migrations.AlterField(
            model_name="product",
            name="barcode",
            field=models.CharField(blank=True, max_length=50, null=True, verbose_name="Штрих-код"),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.UniqueConstraint(
                condition=Q(company__isnull=False),
                fields=("company", "phone"),
                name="uniq_company_customer_phone",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                condition=Q(company__isnull=False, barcode__isnull=False) & ~Q(barcode=""),
                fields=("company", "barcode"),
                name="uniq_company_product_barcode",
            ),
        ),
    ]
