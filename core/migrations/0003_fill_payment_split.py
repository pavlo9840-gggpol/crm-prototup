from django.db import migrations


def fill(apps, schema_editor):
    Sale = apps.get_model("core", "Sale")
    for s in Sale.objects.filter(status="done", cash_amount=0, card_amount=0):
        if s.payment_method == "card":
            s.card_amount = s.total
        else:
            s.cash_amount = s.total
        s.save(update_fields=["cash_amount", "card_amount"])


class Migration(migrations.Migration):
    dependencies = [("core", "0002_cash_company_docs")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
