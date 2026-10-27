from django.db import migrations


def seed(apps, schema_editor):
    Currency = apps.get_model("core", "Currency")
    for code, name, sym, base in (("UAH", "Гривня", "₴", True), ("EUR", "Євро", "€", False), ("USD", "Долар США", "$", False),
                                  ("PLN", "Злотий", "zł", False)):
        Currency.objects.get_or_create(code=code, defaults={"name": name, "symbol": sym, "is_base": base})


class Migration(migrations.Migration):
    dependencies = [("core", "0018_currencies")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
