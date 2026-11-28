from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_license_workstation_sessions"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="company",
            name="plan",
        ),
        migrations.RemoveField(
            model_name="company",
            name="status",
        ),
        migrations.RemoveField(
            model_name="company",
            name="trial_ends_at",
        ),
        migrations.RemoveField(
            model_name="company",
            name="subscription_ends_at",
        ),
    ]
