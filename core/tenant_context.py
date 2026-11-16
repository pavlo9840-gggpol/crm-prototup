"""Single-installation store context.

The application is no longer a SaaS multi-tenant service. Each installation has
one local store. Company remains a compatibility/data-owner record because
existing business tables reference it; it is not a customer workspace.
"""
from functools import wraps

from django.http import HttpResponseForbidden


class CompanyContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.company = None
        request.company_membership = None
        request.company_access = False
        request.company_conflict = False

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from .models import Company, CompanyMembership

            stores = Company.objects.order_by("pk")
            # Never silently merge or expose data from multiple old SaaS workspaces.
            if stores.count() == 1:
                company = stores.first()
                membership = CompanyMembership.objects.filter(
                    company=company, user=user, is_active=True
                ).first()
                request.company = company
                request.company_membership = membership
                request.company_access = bool(membership or user.is_superuser)
            elif stores.count() > 1:
                request.company_conflict = True

        return self.get_response(request)


def company_queryset(queryset, request, *, include_unassigned=False):
    """Scope operational data to the one local store in this installation."""
    company_id = getattr(getattr(request, "company", None), "pk", None)
    if company_id is None or not getattr(request, "company_access", False):
        return queryset.none()
    return queryset.filter(company_id=company_id)


def company_required(view_func):
    """Require an initialized local store and an active local staff account."""
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if (getattr(request, "company", None) is None
                or not getattr(request, "company_access", False)):
            return HttpResponseForbidden(
                "Локальний магазин не налаштований або обліковий запис не має доступу."
            )
        return view_func(request, *args, **kwargs)
    return wrapped


class InstallationSetupMiddleware:
    """Send a fresh database to the one-time local setup page."""
    EXEMPT_PREFIXES = ("/setup/", "/static/", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.EXEMPT_PREFIXES):
            return self.get_response(request)

        from django.contrib.auth import get_user_model
        from .models import Company
        if not get_user_model().objects.exists() or not Company.objects.exists():
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect("/setup/")
        return self.get_response(request)
