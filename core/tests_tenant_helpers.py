"""Small helpers for explicit workspace membership in tenant-aware tests."""
from core.models import Company, CompanyMembership


def create_test_company(owner, name="Test workspace", members=()):
    company = Company.objects.create(
        name=name,
        slug=f"test-workspace-{owner.pk}",
        status="active",
        owner=owner,
    )
    CompanyMembership.objects.create(company=company, user=owner, role="owner")
    for user, role in members:
        CompanyMembership.objects.get_or_create(company=company, user=user, defaults={"role": role})
    return company
