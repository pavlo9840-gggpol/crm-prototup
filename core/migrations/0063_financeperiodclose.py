import django.conf
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0062_bankaccount_opening_balance"),
        migrations.swappable_dependency(django.conf.settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FinancePeriodClose",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date_from", models.DateField(verbose_name="Початок періоду")),
                ("date_to", models.DateField(verbose_name="Кінець періоду")),
                ("snapshot", models.JSONField(default=dict, verbose_name="Знімок показників")),
                ("checksum", models.CharField(max_length=64, verbose_name="SHA-256 знімка")),
                ("closed_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата закриття")),
                ("closed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="finance_period_closes", to=django.conf.settings.AUTH_USER_MODEL)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="finance_period_closes", to="core.company")),
            ],
            options={
                "ordering": ["-closed_at", "-pk"],
                "constraints": [
                    models.UniqueConstraint(fields=("company", "date_from", "date_to"), name="uniq_finance_period_close_range"),
                    models.CheckConstraint(condition=models.Q(("date_to__gte", models.F("date_from"))), name="finance_period_close_valid_range"),
                ],
            },
        ),
    ]
