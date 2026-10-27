from django import forms
from .models import CompanySettings, Category, Customer, Product, StockAdjustment, Supplier, norm_sku


class ProductForm(forms.ModelForm):
    """Картка товару для автозапчастин: однозначно товар визначають БРЕНД + АРТИКУЛ."""
    initial_stock = forms.DecimalField(label="Початковий залишок", required=False, min_value=0,
                                       max_digits=12, decimal_places=3)
    field_order = ["brand", "sku", "name", "category", "unit", "cost_price", "price", "initial_stock", "min_stock",
                   "barcode", "photo", "is_favorite", "allow_price_change", "is_active", "notes"]

    class Meta:
        model = Product
        fields = ["brand", "sku", "name", "category", "unit", "cost_price", "price", "min_stock", "barcode", "photo",
                  "is_favorite", "allow_price_change", "auto_price", "is_active", "notes"]
        labels = {"sku": "Артикул (номер запчастини)", "barcode": "Штрих-код (необов'язково)"}
        help_texts = {"brand": "Оберіть зі списку або введіть нового виробника. Бренд і артикул разом однозначно "
                               "визначають товар: однаковий артикул у різних брендів — це різні товари.",
                      "barcode": "Для запчастин зазвичай не потрібен: товар шукають за артикулом."}
        widgets = {"brand": forms.TextInput(attrs={"list": "brand-list", "autocomplete": "off",
                                                    "placeholder": "напр. BILSTEIN"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["brand"].required = True
        if self.instance.pk:
            del self.fields["initial_stock"]

    def clean_brand(self):
        return " ".join(self.cleaned_data["brand"].split()).upper()

    def clean_sku(self):
        return self.cleaned_data["sku"].strip()

    def clean(self):
        d = super().clean()
        brand, sku = d.get("brand"), d.get("sku")
        if brand and sku:
            dup = Product.objects.filter(sku_norm=norm_sku(sku), brand__iexact=brand)
            if self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            dup = dup.first()
            if dup:
                self.add_error("sku", f"Такий товар уже є в базі: {dup.brand} {dup.sku} — «{dup.name}». "
                                      f"Артикули порівнюються без пробілів і дефісів.")
        return d


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = "__all__"


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = "__all__"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["rate_margin_pct"].required = False          # має значення за замовчуванням (0)

    def clean_rate_margin_pct(self):
        v = self.cleaned_data.get("rate_margin_pct")
        return v if v is not None else 0

    def clean_fixed_rate(self):
        v = self.cleaned_data.get("fixed_rate")
        if v is not None and v <= 0:
            raise forms.ValidationError("Курс має бути більшим за 0")
        return v


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "markup_tier", "birthday", "notes"]
        widgets = {"birthday": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}


class AdjustForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ["product", "kind", "quantity", "reason", "note"]

    def clean_quantity(self):
        q = self.cleaned_data["quantity"]
        if q <= 0:
            raise forms.ValidationError("Кількість має бути більше нуля")
        return q


class CompanyForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = "__all__"
        widgets = {"receipt_header": forms.Textarea(attrs={"rows": 2}), "receipt_footer": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["pickup_days"].required = False       # порожнє = лишається 14 днів за замовчуванням
