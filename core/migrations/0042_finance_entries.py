# Generated for the management finance journal.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0041_inventory_counts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FinanceEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entry_type", models.CharField(choices=[("income", "Дохід"), ("expense", "Витрата")], db_index=True, max_length=10, verbose_name="Тип операції")),
                ("category", models.CharField(max_length=80, verbose_name="Категорія")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Сума")),
                ("payment_method", models.CharField("Спосіб оплати", choices=[("cash", "Готівка"), ("card", "Картка"), ("bank", "Банківський рахунок")], default="cash", max_length=10)),
                ("description", models.CharField(blank=True, max_length=250, verbose_name="Опис")),
                ("document_ref", models.CharField(blank=True, max_length=80, verbose_name="Номер документа")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата створення")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="finance_entries", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="finance_entries", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(model_name="financeentry", index=models.Index(fields=["company", "created_at"], name="finance_company_date_idx")),
        migrations.AddIndex(model_name="financeentry", index=models.Index(fields=["company", "entry_type", "created_at"], name="finance_company_type_idx")),
        migrations.AddConstraint(model_name="financeentry", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="finance_entry_amount_positive")),
    ]
