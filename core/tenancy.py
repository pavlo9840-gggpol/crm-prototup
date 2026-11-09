"""Tenant onboarding primitives. Data isolation is intentionally introduced in a later phase."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import Company, CompanyMembership


@transaction.atomic
def create_company(owner, name, *, slug=None, trial_days=14, plan="start"):
    """Create a workspace and its owner membership as one atomic operation."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Вкажіть назву компанії.")
    if trial_days < 0:
        raise ValueError("Тривалість пробного періоду не може бути від'ємною.")
    base = slugify(slug or clean_name)[:170].strip("-") or "shop"
    candidate = base
    suffix = 2
    while Company.objects.filter(slug=candidate).exists():
        tail = f"-{suffix}"
        candidate = f"{base[:180-len(tail)]}{tail}"
        suffix += 1

    company = Company.objects.create(
        name=clean_name,
        slug=candidate,
        owner=owner,
        plan=plan,
        status="trial",
        trial_ends_at=timezone.now() + timedelta(days=trial_days),
    )
    CompanyMembership.objects.create(company=company, user=owner, role="owner")
    return company
