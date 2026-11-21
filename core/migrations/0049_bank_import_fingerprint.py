from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0048_account_advances"),
    ]

    operations = [
        migrations.AddField(
            model_name="banktransaction",
            name="import_fingerprint",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
    ]
