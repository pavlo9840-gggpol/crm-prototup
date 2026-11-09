from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Company, CompanyMembership
from core.tenancy import create_company


class TenantFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tenant-owner", password="secret")

    def test_onboarding_creates_company_and_owner_membership_atomically(self):
        company = create_company(self.user, "Магазин АвтоЗапас")

        self.assertEqual(company.owner, self.user)
        self.assertEqual(company.plan, "start")
        self.assertEqual(company.status, "trial")
        self.assertTrue(company.has_access)
        membership = CompanyMembership.objects.get(company=company, user=self.user)
        self.assertEqual(membership.role, "owner")
        self.assertTrue(membership.is_company_admin)

    def test_company_slug_is_unique_and_generated_deterministically(self):
        first = create_company(self.user, "Авто Запас", slug="avto-zapas")
        second = create_company(self.user, "Авто Запас")

        self.assertEqual(first.slug, "avto-zapas")
        self.assertEqual(second.slug, "avto-zapas-2")

    def test_expired_trial_and_suspended_company_have_no_access(self):
        company = create_company(self.user, "Тестовий магазин")
        company.trial_ends_at = timezone.now() - timedelta(seconds=1)
        company.save(update_fields=["trial_ends_at"])
        self.assertFalse(company.has_access)

        company.status = "suspended"
        company.trial_ends_at = timezone.now() + timedelta(days=5)
        company.save(update_fields=["status", "trial_ends_at"])
        self.assertFalse(company.has_access)

    def test_duplicate_membership_is_rejected_by_database_constraint(self):
        company = create_company(self.user, "Унікальне членство")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompanyMembership.objects.create(company=company, user=self.user, role="admin")
