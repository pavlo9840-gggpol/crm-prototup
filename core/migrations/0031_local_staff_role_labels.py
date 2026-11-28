from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0030_local_license_activation"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="companymembership",
            options={
                "ordering": ["company_id", "user_id"],
                "verbose_name": "Роль працівника",
                "verbose_name_plural": "Ролі працівників",
            },
        ),
    ]
