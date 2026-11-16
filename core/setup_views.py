from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.shortcuts import redirect, render

from .models import Company, CompanyMembership


class InstallationSetupForm(forms.Form):
    shop_name = forms.CharField(label="Назва магазину", max_length=150)
    username = forms.CharField(label="Логін адміністратора", max_length=150)
    email = forms.EmailField(label="Email", required=False)
    password = forms.CharField(label="Пароль", widget=forms.PasswordInput)
    password_confirm = forms.CharField(label="Повторіть пароль", widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        User = get_user_model()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Такий логін уже існує.")
        return username

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("password_confirm")
        if password and confirm and password != confirm:
            self.add_error("password_confirm", "Паролі не збігаються.")
        if password:
            try:
                validate_password(password)
            except ValidationError as exc:
                self.add_error("password", exc)
        return cleaned


def installation_setup(request):
    User = get_user_model()
    user_count = User.objects.count()
    companies = Company.objects.order_by("pk")

    if user_count and not request.user.is_authenticated:
        return redirect("login")
    if user_count and request.user.is_authenticated and not (
        request.user.is_superuser or request.user.is_staff
    ):
        return redirect("dashboard")
    if companies.count() > 1:
        return render(request, "core/setup.html", {
            "conflict": True,
            "form": None,
        }, status=409)
    if user_count and companies.exists():
        return redirect("dashboard")

    form = InstallationSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            if user_count:
                owner = request.user
            else:
                owner = User.objects.create_superuser(
                    username=data["username"],
                    email=data.get("email", ""),
                    password=data["password"],
                )

            company = companies.first()
            if company is None:
                company = Company.objects.create(
                    name=data["shop_name"].strip(),
                    slug="local-autozapaz",
                    owner=owner,
                    plan="start",
                    status="active",
                )
            else:
                company.name = data["shop_name"].strip()
                company.owner = owner
                company.status = "active"
                company.save(update_fields=["name", "owner", "status"])

            CompanyMembership.objects.update_or_create(
                company=company,
                user=owner,
                defaults={"role": "owner", "is_active": True},
            )

        if not user_count:
            login(request, owner)
        messages.success(request, "Початкове налаштування магазину завершено.")
        return redirect("dashboard")

    return render(request, "core/setup.html", {"form": form, "conflict": False})
