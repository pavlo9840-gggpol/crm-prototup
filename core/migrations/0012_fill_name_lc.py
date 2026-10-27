from django.db import migrations


def fill(apps, schema_editor):
    for label in ("Product", "SupplierItem", "Customer"):
        M = apps.get_model("core", label)
        batch = []
        for o in M.objects.all().only("pk", "name").iterator(chunk_size=2000):
            o.name_lc = (o.name or "").lower()
            batch.append(o)
            if len(batch) >= 1000:
                M.objects.bulk_update(batch, ["name_lc"])
                batch = []
        if batch:
            M.objects.bulk_update(batch, ["name_lc"])


class Migration(migrations.Migration):
    dependencies = [("core", "0011_name_lc")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
