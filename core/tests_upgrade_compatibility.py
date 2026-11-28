from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class LegacySQLiteUpgradeCompatibilityTests(TransactionTestCase):
    """Simulate an existing pre-local-licensing SQLite installation."""

    migrate_from = ("core", "0033_installation_identity")
    migrate_to = ("core", "0037_remove_legacy_company_membership")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        app_label, model_name = settings.AUTH_USER_MODEL.split(".")
        User = old_apps.get_model(app_label, model_name)
        Company = old_apps.get_model("core", "Company")
        LegacyMembership = old_apps.get_model("core", "CompanyMembership")
        Product = old_apps.get_model("core", "Product")

        owner = User.objects.create(username="upgrade-owner", is_active=True)
        company = Company.objects.create(
            name="Existing customer shop",
            slug="existing-customer-shop",
            owner_id=owner.pk,
            plan="pro",
            status="active",
        )
        membership = LegacyMembership.objects.create(
            company_id=company.pk,
            user_id=owner.pk,
            role="owner",
            is_active=True,
        )
        self.membership_created_at = timezone.now() - timedelta(days=120)
        LegacyMembership.objects.filter(pk=membership.pk).update(
            created_at=self.membership_created_at
        )
        product = Product.objects.create(
            company_id=company.pk,
            name="Legacy water pump",
            sku="WP-LEGACY-001",
        )
        self.company_id = company.pk
        self.owner_id = owner.pk
        self.product_id = product.pk

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_company_membership_and_products_survive_full_local_upgrade(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        latest_apps = MigrationExecutor(connection).loader.project_state(
            [self.migrate_to]
        ).apps

        Company = latest_apps.get_model("core", "Company")
        StaffMembership = latest_apps.get_model("core", "StaffMembership")
        Product = latest_apps.get_model("core", "Product")

        company = Company.objects.get(pk=self.company_id)
        self.assertEqual(company.name, "Existing customer shop")
        self.assertEqual(company.owner_id, self.owner_id)
        self.assertFalse(any(
            field.name in {"plan", "status", "trial_ends_at", "subscription_ends_at"}
            for field in Company._meta.fields
        ))

        membership = StaffMembership.objects.get(
            company_id=self.company_id,
            user_id=self.owner_id,
        )
        self.assertEqual(membership.role, "owner")
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.created_at, self.membership_created_at)

        product = Product.objects.get(pk=self.product_id)
        self.assertEqual(product.company_id, self.company_id)
        self.assertEqual(product.name, "Legacy water pump")
        self.assertEqual(product.sku, "WP-LEGACY-001")

        table_names = set(connection.introspection.table_names())
        self.assertIn("core_staffmembership", table_names)
        self.assertNotIn("core_companymembership", table_names)
        self.assertIn("core_licenseworkstationsession", table_names)
