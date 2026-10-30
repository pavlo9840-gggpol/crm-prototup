from django import forms
from .service_models import ServiceAppointment, ServiceOrder, ServiceOrderLine


class ServiceAppointmentForm(forms.ModelForm):
    scheduled_at = forms.DateTimeField(label="Дата й час", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    class Meta:
        model = ServiceAppointment
        fields = ["customer", "car", "scheduled_at", "duration_minutes", "complaint", "assigned_to"]
        widgets = {"complaint": forms.Textarea(attrs={"rows": 3})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_at"].input_formats = ["%Y-%m-%dT%H:%M"]
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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.data.get("customer"):
            self.fields["car"].queryset = self.fields["car"].queryset.filter(customer_id=self.data["customer"])
        elif self.instance.pk:
            self.fields["car"].queryset = self.fields["car"].queryset.filter(customer=self.instance.customer)


class ServiceOrderLineForm(forms.ModelForm):
    class Meta:
        model = ServiceOrderLine
        fields = ["kind", "description", "product", "quantity", "unit_price"]
