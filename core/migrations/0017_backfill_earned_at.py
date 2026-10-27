from django.db import migrations
from django.db.models import F


def fill(apps, schema_editor):
    """Наявні продажі: зі складу зароблено в момент чека; видані клієнтам замовлення: коли прибули (або в момент чека)."""
    SaleItem = apps.get_model("core", "SaleItem")
    for it in SaleItem.objects.filter(earned_at__isnull=True, sale__status="done").select_related("sale").iterator(chunk_size=2000):
        if it.source == "supplier":
            if it.fulfil_status != "issued":
                continue
            when = it.arrived_at or it.sale.created_at
        else:
            when = it.sale.created_at
        SaleItem.objects.filter(pk=it.pk).update(earned_at=when)


class Migration(migrations.Migration):
    dependencies = [("core", "0016_earned_at")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
