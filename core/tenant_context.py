"""Resolve the active workspace for each authenticated request.

This middleware deliberately does not silently filter ORM queries: each view/service
must opt into company-scoped querysets before tenant isolation can be enabled safely.
"""
from functools import wraps

from django.db.models import Q
from django.http import HttpResponseForbidden


class CompanyContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.company = None
        request.company_membership = None
        request.company_access = False

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from .models import CompanyMembership

            memberships = (
                CompanyMembership.objects
                .select_related("company")
                .filter(user=user, is_active=True)
                .filter(Q(company__status="active") | Q(company__status="trial"))
                .order_by("company_id")
            )
            selected_id = request.session.get("active_company_id")
            membership = memberships.filter(company_id=selected_id).first() if selected_id else None

            if membership is None and memberships.count() == 1:
                membership = memberships.first()
                request.session["active_company_id"] = membership.company_id

            if membership is not None:
                request.company_membership = membership
                request.company = membership.company
                request.company_access = membership.company.has_access

        return self.get_response(request)


def company_queryset(queryset, request, *, include_unassigned=False):
    """Explicitly scope a queryset; returns no rows when request has no active workspace."""
    company_id = getattr(getattr(request, "company", None), "pk", None)
    if company_id is None or not getattr(request, "company_access", False):
        return queryset.none()
    return queryset.filter(company_id=company_id)


def company_required(view_func):
    """Reject views without an active, entitled workspace."""
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if getattr(request, "company", None) is None or not getattr(request, "company_access", False):
            return HttpResponseForbidden("Немає доступу до активного магазину.")
        return view_func(request, *args, **kwargs)
    return wrapped
