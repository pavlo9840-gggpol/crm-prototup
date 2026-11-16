from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_company_scoped_payroll_and_legacy_docs"),
    ]

    operations = [
        migrations.CreateModel(
            name="LicenseActivation",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("token", models.TextField(verbose_name="Підписаний ліцензійний ключ")),
                ("license_id", models.CharField(max_length=80, unique=True, verbose_name="ID ліцензії")),
                ("plan", models.CharField(choices=[("start", "START"), ("pro", "PRO"), ("business", "BUSINESS")], max_length=20, verbose_name="Тариф")),
                ("issued_at", models.DateTimeField(verbose_name="Видано")),
                ("expires_at", models.DateTimeField(blank=True, null=True, verbose_name="Діє до")),
                ("max_users", models.PositiveIntegerField(default=1, verbose_name="Максимум користувачів")),
                ("max_workstations", models.PositiveIntegerField(default=1, verbose_name="Максимум робочих місць")),
                ("features", models.JSONField(blank=True, default=list, verbose_name="Доступні модулі")),
                ("activated_at", models.DateTimeField(auto_now_add=True, verbose_name="Активовано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Оновлено")),
            ],
            options={
                "verbose_name": "Локальна ліцензія",
                "verbose_name_plural": "Локальна ліцензія",
            },
        ),
    ]
