from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_company_ownership_backfill"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="companysettings",
            constraint=models.UniqueConstraint(
                fields=("company",),
                condition=models.Q(company__isnull=False),
                name="uniq_company_settings",
            ),
        ),
    ]
