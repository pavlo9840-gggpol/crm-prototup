"""Small helpers for explicit workspace membership in tenant-aware tests."""
from core.models import Company, StaffMembership


def create_test_company(owner, name="Test workspace", members=()):
    company = Company.objects.create(
        name=name,
        slug=f"test-workspace-{owner.pk}",
        owner=owner,
    )
    StaffMembership.objects.create(company=company, user=owner, role="owner")
    for user, role in members:
        StaffMembership.objects.get_or_create(company=company, user=user, defaults={"role": role})
    return company
