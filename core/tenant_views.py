from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import CompanyMembership
from .tenancy import create_company


@login_required
def create_workspace(request):
    if request.method == "POST":
        name = request.POST.get("name", "")
        try:
            company = create_company(request.user, name)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            request.session["active_company_id"] = company.pk
            messages.success(request, f"Магазин «{company.name}» створено. Пробний період — 14 днів.")
            return redirect("dashboard")
    return render(request, "core/workspace_new.html")


@login_required
@require_POST
def select_company(request):
    company_id = request.POST.get("company_id")
    membership = get_object_or_404(
        CompanyMembership.objects.select_related("company"),
        company_id=company_id,
        user=request.user,
        is_active=True,
    )
    if not membership.company.has_access:
        messages.error(request, "Доступ до цього магазину призупинено або завершився.")
        return redirect("dashboard")

    request.session["active_company_id"] = membership.company_id
    messages.success(request, f"Активний магазин: {membership.company.name}")
    return redirect("dashboard")
