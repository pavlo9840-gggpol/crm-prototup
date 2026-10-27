from django.db import migrations


def fill(apps, schema_editor):
    PartInfo = apps.get_model("core", "PartInfo")
    batch = []
    for o in PartInfo.objects.exclude(cars="").only("pk", "cars").iterator(chunk_size=2000):
        o.cars_lc = (o.cars or "").lower()
        batch.append(o)
        if len(batch) >= 1000:
            PartInfo.objects.bulk_update(batch, ["cars_lc"])
            batch = []
    if batch:
        PartInfo.objects.bulk_update(batch, ["cars_lc"])


class Migration(migrations.Migration):
    dependencies = [("core", "0014_pricing_pay")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
