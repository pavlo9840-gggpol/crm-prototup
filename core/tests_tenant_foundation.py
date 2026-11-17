from django.db import IntegrityError, transaction
from django.test import TestCase
from django.contrib.auth import get_user_model

from core.models import Company, CompanyMembership
from core.tenancy import create_local_store


class LocalInstallationFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="local-owner", password="secret")

    def test_first_setup_creates_one_active_local_store_and_owner_role(self):
        store = create_local_store(self.user, "Магазин АвтоЗапас")

        self.assertEqual(store.owner, self.user)
        self.assertEqual(store.name, "Магазин АвтоЗапас")
        membership = CompanyMembership.objects.get(company=store, user=self.user)
        self.assertEqual(membership.role, "owner")
        self.assertTrue(membership.is_company_admin)

    def test_second_store_creation_is_rejected_in_single_installation(self):
        first = create_local_store(self.user, "АвтоЗапас")
        with self.assertRaises(ValueError):
            create_local_store(self.user, "Другий магазин")
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(Company.objects.first(), first)

    def test_duplicate_local_role_is_rejected_by_database_constraint(self):
        store = create_local_store(self.user, "Унікальне членство")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompanyMembership.objects.create(company=store, user=self.user, role="admin")
