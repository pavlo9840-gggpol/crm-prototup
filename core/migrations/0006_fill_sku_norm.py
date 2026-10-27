import re
from django.db import migrations


def fill(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    for p in Product.objects.all().only("pk", "sku"):
        n = re.sub(r"[\W_]+", "", p.sku or "", flags=re.UNICODE).upper()
        Product.objects.filter(pk=p.pk).update(sku_norm=n)


class Migration(migrations.Migration):
    dependencies = [("core", "0005_transit")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
