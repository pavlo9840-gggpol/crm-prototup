from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_legacy_staff_memberships(apps, schema_editor):
    LegacyMembership = apps.get_model("core", "CompanyMembership")
    StaffMembership = apps.get_model("core", "StaffMembership")
    alias = schema_editor.connection.alias

    for legacy in LegacyMembership.objects.using(alias).all().iterator():
        membership, created = StaffMembership.objects.using(alias).get_or_create(
            company_id=legacy.company_id,
            user_id=legacy.user_id,
            defaults={
                "role": legacy.role,
                "is_active": legacy.is_active,
            },
        )
        if created:
            # auto_now_add overrides a supplied timestamp on insert; restore the
            # original legacy timestamp explicitly after the row is created.
            StaffMembership.objects.using(alias).filter(pk=membership.pk).update(
                created_at=legacy.created_at
            )


def reverse_copy_is_noop(apps, schema_editor):
    # Keep the legacy table intact; reversing this migration must not delete staff data.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_remove_legacy_company_subscription_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[
                    ("owner", "Власник"),
                    ("admin", "Адміністратор"),
                    ("manager", "Менеджер"),
                    ("cashier", "Касир"),
                    ("mechanic", "Механік"),
                ], default="cashier", max_length=20)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="staff_memberships",
                    to="core.company",
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="staff_memberships",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "verbose_name": "Роль працівника",
                "verbose_name_plural": "Ролі працівників",
                "ordering": ["company_id", "user_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="staffmembership",
            constraint=models.UniqueConstraint(fields=("company", "user"), name="uniq_company_staff_membership"),
        ),
        migrations.AddIndex(
            model_name="staffmembership",
            index=models.Index(fields=["user", "is_active"], name="sm_user_active_idx"),
        ),
        migrations.RunPython(copy_legacy_staff_memberships, reverse_copy_is_noop),
    ]
