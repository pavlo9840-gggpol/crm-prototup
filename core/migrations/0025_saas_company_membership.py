# Initial SaaS workspace identity layer. Existing shop data remains untouched.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0024_sale_return_refund_settlement"),
    ]

    operations = [
        migrations.CreateModel(
            name="Company",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=150, verbose_name="Назва магазину")),
                ("slug", models.SlugField(max_length=180, unique=True, verbose_name="Ідентифікатор")),
                ("plan", models.CharField(choices=[("start", "START"), ("business", "BUSINESS"), ("pro", "PRO"), ("network", "NETWORK")], db_index=True, default="start", max_length=20)),
                ("status", models.CharField(choices=[("trial", "Пробний період"), ("active", "Активна"), ("suspended", "Призупинена"), ("cancelled", "Скасована")], db_index=True, default="trial", max_length=20)),
                ("trial_ends_at", models.DateTimeField(blank=True, null=True)),
                ("subscription_ends_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="owned_companies", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["name"], "verbose_name": "Компанія", "verbose_name_plural": "Компанії"},
        ),
        migrations.CreateModel(
            name="CompanyMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("owner", "Власник"), ("admin", "Адміністратор"), ("manager", "Менеджер"), ("cashier", "Касир"), ("mechanic", "Механік")], default="cashier", max_length=20)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="core.company")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="company_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["company_id", "user_id"], "verbose_name": "Учасник компанії", "verbose_name_plural": "Учасники компаній"},
        ),
        migrations.AddConstraint(
            model_name="companymembership",
            constraint=models.UniqueConstraint(fields=("company", "user"), name="uniq_company_user_membership"),
        ),
        migrations.AddIndex(
            model_name="companymembership",
            index=models.Index(fields=["user", "is_active"], name="cm_user_active_idx"),
        ),
    ]
