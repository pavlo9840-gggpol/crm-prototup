from django.db import migrations


def keep_staff_memberships(apps, schema_editor):
    # Forward migration deliberately keeps StaffMembership as the source of truth.
    pass


def restore_legacy_memberships(apps, schema_editor):
    """Rebuild the legacy table from the current staff roles when rolling back."""
    StaffMembership = apps.get_model("core", "StaffMembership")
    CompanyMembership = apps.get_model("core", "CompanyMembership")
    alias = schema_editor.connection.alias

    for staff in StaffMembership.objects.using(alias).all().iterator():
        legacy, created = CompanyMembership.objects.using(alias).get_or_create(
            company_id=staff.company_id,
            user_id=staff.user_id,
            defaults={
                "role": staff.role,
                "is_active": staff.is_active,
            },
        )
        if created:
            CompanyMembership.objects.using(alias).filter(pk=legacy.pk).update(
                created_at=staff.created_at
            )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0036_local_staff_membership"),
    ]

    operations = [
        migrations.RunPython(keep_staff_memberships, restore_legacy_memberships),
        migrations.DeleteModel(name="CompanyMembership"),
    ]
