from django import forms
from django.contrib.auth import get_user_model
from .service_models import ServiceAppointment, ServiceOrder, ServiceOrderLine


class ServiceAppointmentForm(forms.ModelForm):
    scheduled_at = forms.DateTimeField(label="Дата й час", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    class Meta:
        model = ServiceAppointment
        fields = ["customer", "car", "scheduled_at", "duration_minutes", "complaint", "assigned_to"]
        widgets = {"complaint": forms.Textarea(attrs={"rows": 3})}
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        if company is not None:
            from .models import Customer, CustomerCar
            self.fields["customer"].queryset = Customer.objects.filter(company=company)
            self.fields["car"].queryset = CustomerCar.objects.filter(company=company).select_related("customer")
            self.fields["assigned_to"].queryset = get_user_model().objects.filter(
                company_memberships__company=company, company_memberships__is_active=True
            ).distinct()
        self.fields["car"].queryset = self.fields["car"].queryset.select_related("customer")
        if self.data.get("customer"):
            self.fields["car"].queryset = self.fields["car"].queryset.filter(customer_id=self.data["customer"])
        elif self.instance.pk:
            self.fields["car"].queryset = self.fields["car"].queryset.filter(customer=self.instance.customer)


class ServiceOrderForm(forms.ModelForm):
    class Meta:
        model = ServiceOrder
        fields = ["customer", "car", "appointment", "mechanic", "mileage", "complaint", "diagnosis", "recommendation", "status"]
        widgets = {"complaint": forms.Textarea(attrs={"rows": 2}), "diagnosis": forms.Textarea(attrs={"rows": 3}), "recommendation": forms.Textarea(attrs={"rows": 2})}
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            from .models import Customer, CustomerCar, CompanyMembership
            self.fields["customer"].queryset = Customer.objects.filter(company=company)
            self.fields["car"].queryset = CustomerCar.objects.filter(company=company)
            self.fields["appointment"].queryset = ServiceAppointment.objects.filter(company=company)
            self.fields["mechanic"].queryset = __import__("django.contrib.auth", fromlist=["get_user_model"]).get_user_model().objects.filter(
                company_memberships__company=company, company_memberships__is_active=True
            ).distinct()
        if self.data.get("customer"):
            self.fields["car"].queryset = self.fields["car"].queryset.filter(customer_id=self.data["customer"])
        elif self.instance.pk:
            self.fields["car"].queryset = self.fields["car"].queryset.filter(customer=self.instance.customer)


class ServiceOrderLineForm(forms.ModelForm):
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            from .models import Product
            self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)

    class Meta:
        model = ServiceOrderLine
        fields = ["kind", "description", "product", "quantity", "unit_price"]
