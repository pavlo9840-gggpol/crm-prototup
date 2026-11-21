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
        request.staff_membership = None
        request.company_access = False
        request.company_conflict = False

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from .models import Company, StaffMembership

            memberships = (
                StaffMembership.objects
                .select_related("company")
                .filter(user=user, is_active=True)
                .order_by("company_id")
            )
            # Legacy databases may still contain more than one old workspace.
            # Never offer a switcher; require an explicit data-consolidation step
            # when one local account is attached to multiple stores.
            if memberships.count() == 1:
                membership = memberships.first()
                request.staff_membership = membership
                request.company = membership.company
                request.company_access = True
            elif memberships.count() > 1:
                request.company_conflict = True
            elif user.is_superuser:
                stores = Company.objects.order_by("pk")
                if stores.exists():
                    # Recovery administrators may open the first legacy store
                    # to perform controlled cleanup; ordinary staff are blocked
                    # when their account is attached to multiple stores.
                    request.company = stores.first()
                    request.company_access = True
                    request.company_conflict = stores.count() > 1

        return self.get_response(request)


class BusinessProfileMiddleware:
    """Block industry-specific URLs even when a user enters them manually."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        company = getattr(request, "company", None)
        if company is not None:
            from .business_profiles import required_profile_for_path
            required = required_profile_for_path(request.path)
            if required and company.business_type != required:
                from django.shortcuts import render
                return render(request, "core/business_profile_required.html", {
                    "required_profile": "Автозапчастини",
                    "current_profile": company.get_business_type_display(),
                }, status=403)
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
    EXEMPT_PREFIXES = ("/setup/", "/login/", "/logout/", "/static/", "/media/")

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


class LicenseRequiredMiddleware:
    """Require a valid local license in production builds, never during development."""
    EXEMPT_PREFIXES = ("/activate/", "/setup/", "/login/", "/logout/", "/static/", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .licensing import get_active_license, license_is_required
        if license_is_required() and not request.path.startswith(self.EXEMPT_PREFIXES):
            if get_active_license() is None:
                from django.http import HttpResponseRedirect
                return HttpResponseRedirect("/activate/")
        return self.get_response(request)


class LicenseFeatureMiddleware:
    """Enforce signed plan entitlements on every request, not only in navigation."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.shortcuts import render
        from .license_entitlements import FEATURE_LABELS, feature_for_path, feature_is_enabled
        from .licensing import get_active_license, license_is_required

        claims = get_active_license()
        request.active_license = claims
        feature = feature_for_path(request.path)
        if feature and license_is_required() and claims:
            if not feature_is_enabled(claims, feature):
                pro_features = {
                    "supplier_api", "supplier_prices", "vehicle_catalogue", "crossrefs",
                    "advanced_pricing", "payroll", "nova_poshta", "advanced_analytics",
                }
                return render(request, "core/license_feature_required.html", {
                    "required_feature": FEATURE_LABELS.get(feature, feature),
                    "current_plan": claims["plan"].upper(),
                    "required_plan": "PRO" if feature in pro_features else "BUSINESS",
                }, status=403)
        return self.get_response(request)


class WorkstationLimitMiddleware:
    """Limit concurrently active authenticated browser sessions.

    A browser session is the practical local-server proxy for one workstation.
    Idle sessions are released after 15 minutes; explicit logout releases a
    workstation immediately. This is not a hardware fingerprint.
    """
    IDLE_MINUTES = 15
    HEARTBEAT_SECONDS = 30

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from datetime import timedelta
        from django.db import transaction
        from django.shortcuts import render
        from django.utils import timezone
        from .licensing import get_active_license, license_is_required
        from .models import InstallationIdentity, LicenseWorkstationSession

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return self.get_response(request)
        if not getattr(request, "company_access", False):
            return self.get_response(request)

        session_key = request.session.session_key
        if request.path.startswith("/logout/"):
            if session_key:
                LicenseWorkstationSession.objects.filter(session_key=session_key).delete()
            return self.get_response(request)
        # Owners must be able to replace/upgrade an exhausted license from a
        # second browser even when all licensed seats are currently occupied.
        if request.path.startswith(("/activate/", "/setup/", "/login/", "/static/", "/media/")):
            return self.get_response(request)

        claims = getattr(request, "active_license", None)
        if claims is None:
            claims = get_active_license()
        if not license_is_required() or not claims:
            return self.get_response(request)

        if session_key is None:
            request.session.create()
            session_key = request.session.session_key

        now = timezone.now()
        idle_before = now - timedelta(minutes=self.IDLE_MINUTES)
        heartbeat_before = now - timedelta(seconds=self.HEARTBEAT_SECONDS)

        with transaction.atomic():
            # Serialize admissions on databases that support row-level locks.
            InstallationIdentity.objects.select_for_update().get_or_create(pk=1)
            LicenseWorkstationSession.objects.filter(last_seen__lt=idle_before).delete()
            current = LicenseWorkstationSession.objects.filter(session_key=session_key).first()
            if current:
                if current.last_seen < heartbeat_before or current.user_id != user.pk:
                    current.user = user
                    current.last_seen = now
                    current.save(update_fields=["user", "last_seen"])
            else:
                active_count = LicenseWorkstationSession.objects.count()
                if active_count >= claims["max_workstations"]:
                    return render(request, "core/license_workstation_limit.html", {
                        "current_plan": claims["plan"].upper(),
                        "max_workstations": claims["max_workstations"],
                        "idle_minutes": self.IDLE_MINUTES,
                    }, status=403)
                LicenseWorkstationSession.objects.create(
                    session_key=session_key, user=user, last_seen=now
                )

        return self.get_response(request)
