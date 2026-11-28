from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class StaffMembershipMigrationRoundTripTests(TransactionTestCase):
    """Exercise both the legacy-data copy and the reversible legacy-table removal."""

    migrate_from = ("core", "0035_remove_legacy_company_subscription_fields")
    migrate_copy = ("core", "0036_local_staff_membership")
    migrate_remove = ("core", "0037_remove_legacy_company_membership")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        app_label, model_name = settings.AUTH_USER_MODEL.split(".")
        User = old_apps.get_model(app_label, model_name)
        Company = old_apps.get_model("core", "Company")
        LegacyMembership = old_apps.get_model("core", "CompanyMembership")

        user = User.objects.create(username="legacy-staff")
        company = Company.objects.create(
            name="Local migration test",
            slug="local-migration-test",
            owner_id=user.pk,
        )
        legacy = LegacyMembership.objects.create(
            company_id=company.pk,
            user_id=user.pk,
            role="mechanic",
            is_active=False,
        )
        self.old_created_at = timezone.now() - timedelta(days=90)
        LegacyMembership.objects.filter(pk=legacy.pk).update(
            created_at=self.old_created_at
        )
        self.company_id = company.pk
        self.user_id = user.pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_copy])
        self.apps_0036 = MigrationExecutor(connection).loader.project_state(
            [self.migrate_copy]
        ).apps

    def tearDown(self):
        # Leave the test database at the latest migration even if an assertion fails.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_copy_is_idempotent_and_table_removal_can_be_rolled_back(self):
        StaffMembership = self.apps_0036.get_model("core", "StaffMembership")
        copied = StaffMembership.objects.get(
            company_id=self.company_id,
            user_id=self.user_id,
        )
        self.assertEqual(copied.role, "mechanic")
        self.assertFalse(copied.is_active)
        self.assertEqual(copied.created_at, self.old_created_at)

        # Re-running the 0036 data migration must not overwrite a newer role.
        migration_0036 = import_module("core.migrations.0036_local_staff_membership")
        migration_0036.copy_legacy_staff_memberships(
            self.apps_0036,
            type("SchemaEditor", (), {"connection": connection})(),
        )
        copied.refresh_from_db()
        self.assertEqual(copied.role, "mechanic")

        # Simulate a live role change, then remove the legacy table.
        StaffMembership.objects.filter(pk=copied.pk).update(
            role="manager",
            is_active=True,
        )
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_remove])

        # Rolling back 0037 recreates CompanyMembership from StaffMembership.
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_copy])
        restored_apps = MigrationExecutor(connection).loader.project_state(
            [self.migrate_copy]
        ).apps
        LegacyMembership = restored_apps.get_model("core", "CompanyMembership")
        restored = LegacyMembership.objects.get(
            company_id=self.company_id,
            user_id=self.user_id,
        )
        self.assertEqual(restored.role, "manager")
        self.assertTrue(restored.is_active)
        self.assertEqual(restored.created_at, self.old_created_at)
