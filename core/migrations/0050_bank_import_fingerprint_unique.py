from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0049_bank_import_fingerprint"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="banktransaction",
            constraint=models.UniqueConstraint(fields=("company", "import_fingerprint"), name="bank_import_fingerprint_unique"),
        ),
    ]
