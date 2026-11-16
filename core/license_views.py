from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils import timezone

from .licensing import LicenseError, verify_license_token
from .models import CompanyMembership, LicenseActivation


@login_required
def activate_license(request):
    membership = getattr(request, "company_membership", None)
    if not request.user.is_superuser and not request.user.is_staff and not (
        membership and membership.role in ("owner", "admin")
    ):
        raise PermissionDenied

    current = LicenseActivation.objects.filter(pk=1).first()
    if request.method == "POST":
        token = request.POST.get("license_key", "")
        try:
            claims = verify_license_token(token)
            active_users = CompanyMembership.objects.filter(is_active=True).values("user_id").distinct().count()
            if active_users > claims["max_users"]:
                raise LicenseError(
                    f"У ліцензії дозволено {claims['max_users']} користувач(ів), "
                    f"а в магазині активних облікових записів: {active_users}."
                )
        except LicenseError as exc:
            messages.error(request, str(exc))
        else:
            LicenseActivation.objects.update_or_create(
                pk=1,
                defaults={
                    "token": token.strip(),
                    "license_id": claims["license_id"],
                    "plan": claims["plan"],
                    "issued_at": claims["issued_at"],
                    "expires_at": claims["expires_at"],
                    "max_users": claims["max_users"],
                    "max_workstations": claims["max_workstations"],
                    "features": claims["features"],
                },
            )
            messages.success(request, "Ліцензію АвтоЗапас успішно активовано.")
            return redirect("dashboard")

    return render(request, "core/license_activation.html", {
        "activation": current,
        "today": timezone.localdate(),
    })
