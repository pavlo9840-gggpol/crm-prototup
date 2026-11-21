# Create an auditable manual bank transaction register.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0046_settlement_cash_operations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BankTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("transaction_date", models.DateField(db_index=True, verbose_name="Дата операції")),
                ("direction", models.CharField(choices=[("income", "Надходження"), ("expense", "Списання")], db_index=True, max_length=10, verbose_name="Напрямок")),
                ("category", models.CharField(max_length=80, verbose_name="Категорія")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Сума")),
                ("counterparty", models.CharField(blank=True, max_length=150, verbose_name="Контрагент")),
                ("document_ref", models.CharField(blank=True, max_length=120, verbose_name="Документ / призначення")),
                ("note", models.CharField(blank=True, max_length=250, verbose_name="Примітка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("is_void", models.BooleanField(db_index=True, default=False, verbose_name="Скасовано")),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("void_reason", models.CharField(blank=True, max_length=250)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="transactions", to="core.bankaccount")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bank_transactions", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bank_transactions", to=settings.AUTH_USER_MODEL)),
                ("voided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="voided_bank_transactions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-transaction_date", "-id"]},
        ),
        migrations.AddIndex(model_name="banktransaction", index=models.Index(fields=["company", "account", "transaction_date"], name="bank_tx_account_date_idx")),
        migrations.AddIndex(model_name="banktransaction", index=models.Index(fields=["company", "direction", "transaction_date"], name="bank_tx_direction_date_idx")),
        migrations.AddConstraint(model_name="banktransaction", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="bank_transaction_amount_positive")),
    ]
