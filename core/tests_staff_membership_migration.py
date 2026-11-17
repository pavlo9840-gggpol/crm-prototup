from datetime import timedelta
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import Company, CompanyMembership, StaffMembership


class StaffMembershipDataCopyTests(TestCase):
    def test_legacy_roles_are_copied_without_changing_role_active_state_or_timestamp(self):
        user = get_user_model().objects.create_user(username="legacy-staff", password="test-password")
        company = Company.objects.create(name="Local store", slug="local-store", owner=user)
        legacy = CompanyMembership.objects.create(
            company=company,
            user=user,
            role="mechanic",
            is_active=False,
        )
        old_created_at = timezone.now() - timedelta(days=90)
        CompanyMembership.objects.filter(pk=legacy.pk).update(created_at=old_created_at)

        migration = import_module("core.migrations.0036_local_staff_membership")
        migration.copy_legacy_staff_memberships(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )

        copied = StaffMembership.objects.get(company=company, user=user)
        self.assertEqual(copied.role, "mechanic")
        self.assertFalse(copied.is_active)
        self.assertEqual(copied.created_at, old_created_at)

    def test_copy_is_idempotent_and_does_not_overwrite_new_staff_role(self):
        user = get_user_model().objects.create_user(username="existing-staff", password="test-password")
        company = Company.objects.create(name="Local store 2", slug="local-store-2", owner=user)
        CompanyMembership.objects.create(company=company, user=user, role="cashier")
        StaffMembership.objects.create(company=company, user=user, role="admin", is_active=True)

        migration = import_module("core.migrations.0036_local_staff_membership")
        migration.copy_legacy_staff_memberships(
            __import__("django").apps.apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )

        self.assertEqual(
            StaffMembership.objects.get(company=company, user=user).role,
            "admin",
        )
