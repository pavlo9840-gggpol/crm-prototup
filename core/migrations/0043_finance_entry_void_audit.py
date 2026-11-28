from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0042_finance_entries"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="financeentry",
            name="is_void",
            field=models.BooleanField(db_index=True, default=False, verbose_name="Скасовано"),
        ),
        migrations.AddField(
            model_name="financeentry",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Дата скасування"),
        ),
        migrations.AddField(
            model_name="financeentry",
            name="voided_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="voided_finance_entries", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="financeentry",
            name="void_reason",
            field=models.CharField(blank=True, max_length=250, verbose_name="Причина скасування"),
        ),
    ]
