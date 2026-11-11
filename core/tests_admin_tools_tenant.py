from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.admin_tools import apply_rows, parse_csv
from core.models import CompanyMembership, Product
from core.tests_tenant_helpers import create_test_company


User = get_user_model()


class WorkspaceAdminToolsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="workspace-owner", password="secret")
        self.company = create_test_company(self.owner, "Workspace admin tools")
        self.other_owner = User.objects.create_user(username="other-owner", password="secret")
        self.other_company = create_test_company(self.other_owner, "Other admin tools")
        self.client.force_login(self.owner)

    def test_company_admin_creates_a_local_member_without_global_staff_privileges(self):
        response = self.client.post(reverse("user_new"), {
            "username": "shop-manager",
            "first_name": "Менеджер",
            "role": "manager",
            "is_active": "on",
            "password": "secret123",
        })
        self.assertEqual(response.status_code, 302)
        member = User.objects.get(username="shop-manager")
        self.assertFalse(member.is_staff)
        self.assertFalse(member.is_superuser)
        membership = CompanyMembership.objects.get(company=self.company, user=member)
        self.assertEqual(membership.role, "manager")
        self.assertTrue(membership.is_active)

    def test_editing_member_role_does_not_change_other_company_membership(self):
        member = User.objects.create_user(username="shared-employee", password="secret")
        own = CompanyMembership.objects.create(company=self.company, user=member, role="cashier")
        foreign = CompanyMembership.objects.create(company=self.other_company, user=member, role="mechanic")
        response = self.client.post(reverse("user_edit", args=[member.pk]), {
            "username": member.username,
            "first_name": member.first_name,
            "role": "admin",
            "is_active": "on",
            "password": "",
        })
        self.assertEqual(response.status_code, 302)
        own.refresh_from_db()
        foreign.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(own.role, "admin")
        self.assertEqual(foreign.role, "mechanic")
        self.assertFalse(member.is_staff)
        self.assertFalse(member.is_superuser)

    def test_user_list_and_edit_never_expose_foreign_company_members(self):
        foreign = User.objects.create_user(username="foreign-employee", password="secret")
        CompanyMembership.objects.create(company=self.other_company, user=foreign, role="cashier")
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "foreign-employee")
        self.assertEqual(self.client.get(reverse("user_edit", args=[foreign.pk])).status_code, 404)

    def test_csv_import_export_and_pk_updates_are_workspace_scoped(self):
        own_product = Product.objects.create(company=self.company, name="Своя деталь", sku="OWN-CSV")
        foreign_product = Product.objects.create(company=self.other_company, name="Чужа деталь", sku="FOREIGN-CSV")
        raw = "Код;Назва;Бренд\nNEW-CSV;Нова деталь;TEST\n".encode("utf-8")
        rows, errors, fatal = parse_csv(raw, self.company)
        self.assertIsNone(fatal)
        self.assertEqual(errors, [])
        created, updated = apply_rows(rows, self.owner, self.company)
        self.assertEqual((created, updated), (1, 0))
        self.assertTrue(Product.objects.filter(company=self.company, sku="NEW-CSV").exists())
        with self.assertRaises(ValueError):
            apply_rows([{"pk": foreign_product.pk, "sku": "FOREIGN-CSV", "name": "Підміна"}],
                       self.owner, self.company)
        export = self.client.get(reverse("product_export"))
        self.assertContains(export, "OWN-CSV")
        self.assertContains(export, "NEW-CSV")
        self.assertNotContains(export, "FOREIGN-CSV")
