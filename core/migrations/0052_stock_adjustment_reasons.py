from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0051_bank_reconciliation")]
    operations = [
        migrations.AlterField(
            model_name="stockadjustment",
            name="reason",
            field=models.CharField(blank=True, choices=[("damaged", "Пошкодження"), ("expired", "Прострочення"), ("theft", "Крадіжка"), ("lost", "Втрата"), ("internal", "Внутрішнє використання"), ("other", "Інше")], max_length=10, verbose_name="Причина"),
        ),
    ]
