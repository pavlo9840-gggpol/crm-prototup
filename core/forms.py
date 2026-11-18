from django import forms
from .models import CompanySettings, Category, Customer, Product, ProductBarcode, StockAdjustment, Supplier, norm_sku


class ProductForm(forms.ModelForm):
    """Універсальна картка товару; бренд і артикул залишаються доступними для автозапчастин."""
    initial_stock = forms.DecimalField(label="Початковий залишок", required=False, min_value=0,
                                       max_digits=12, decimal_places=3)
    additional_barcodes = forms.CharField(label="Додаткові штрих-коди", required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Кожен додатковий штрих-код з нового рядка"}),
        help_text="Для упаковок або варіантів одного товару. Основний штрих-код вказується вище.")
    field_order = ["name", "sku", "barcode", "brand", "category", "unit", "product_kind", "tracking_mode", "tax_category", "condition", "has_defect", "condition_description", "cost_price", "price", "initial_stock", "min_stock", "weight_kg", "length_cm", "width_cm", "height_cm", "photo", "photo_url", "is_favorite", "allow_price_change", "is_active", "notes"]

    class Meta:
        model = Product
        fields = ["name", "sku", "barcode", "brand", "category", "unit", "product_kind", "tracking_mode", "tax_category", "condition", "has_defect", "condition_description", "cost_price", "price", "min_stock", "weight_kg", "length_cm", "width_cm", "height_cm", "photo", "photo_url", "is_favorite", "allow_price_change", "auto_price", "is_active", "notes"]
        labels = {"sku": "Артикул / внутрішній код", "barcode": "Штрих-код (необов'язково)"}
        help_texts = {"brand": "Необов’язково. Для автозапчастин бренд і артикул разом однозначно "
                               "визначають товар: однаковий артикул у різних брендів — це різні товари.",
                      "barcode": "Для запчастин зазвичай не потрібен: товар шукають за артикулом."}
        widgets = {"brand": forms.TextInput(attrs={"list": "brand-list", "autocomplete": "off",
                                                    "placeholder": "напр. BILSTEIN"}),
                   "notes": forms.Textarea(attrs={"rows": 2}), "condition_description": forms.Textarea(attrs={"rows": 3}),
                   "photo_url": forms.URLInput(attrs={"placeholder": "https://example.com/photo.jpg"})}

    def __init__(self, *a, company=None, **k):
        super().__init__(*a, **k)
        self.company = company
        if company is not None:
            self.fields["category"].queryset = Category.objects.filter(company=company)
        self.fields["brand"].required = False
        # Нові універсальні поля мають безпечні значення за замовчуванням.
        # Старі клієнти/API та наявні форми, які ще не надсилають ці поля,
        # не повинні раптово ставати невалідними.
        universal_defaults = {
            "product_kind": "goods",
            "tracking_mode": "none",
            "tax_category": "general",
        }
        for field_name, default_value in universal_defaults.items():
            self.fields[field_name].required = False
            self.fields[field_name].initial = (
                getattr(self.instance, field_name) if self.instance.pk else default_value
            )
        if self.instance.pk:
            del self.fields["initial_stock"]

    def clean_brand(self):
        return " ".join(self.cleaned_data["brand"].split()).upper()

    def clean_sku(self):
        return self.cleaned_data["sku"].strip()

    def clean_additional_barcodes(self):
        if self.instance.pk and self.is_bound and "additional_barcodes" not in self.data:
            return list(self.instance.extra_barcodes.values_list("barcode", flat=True))
        raw = self.cleaned_data.get("additional_barcodes") or ""
        codes = []
        seen = set()
        for line in raw.splitlines():
            code = line.strip()
            if not code:
                continue
            if len(code) > 80:
                raise forms.ValidationError("Штрих-код не може бути довшим за 80 символів.")
            if code in seen:
                raise forms.ValidationError(f"Штрих-код {code} повторюється у списку.")
            seen.add(code)
            codes.append(code)
        primary = (self.cleaned_data.get("barcode") or "").strip()
        if primary and primary in seen:
            raise forms.ValidationError("Основний і додатковий штрих-коди мають відрізнятися.")
        if self.company:
            for code in codes:
                primary_match = Product.objects.filter(company=self.company, barcode=code)
                extra_match = ProductBarcode.objects.filter(company=self.company, barcode=code)
                if self.instance.pk:
                    primary_match = primary_match.exclude(pk=self.instance.pk)
                    extra_match = extra_match.exclude(product_id=self.instance.pk)
                if primary_match.exists() or extra_match.exists():
                    raise forms.ValidationError(f"Штрих-код {code} уже закріплений за іншим товаром.")
            if primary:
                extra_match = ProductBarcode.objects.filter(company=self.company, barcode=primary)
                if self.instance.pk:
                    extra_match = extra_match.exclude(product_id=self.instance.pk)
                if extra_match.exists():
                    self.add_error("barcode", "Цей основний штрих-код уже використовується як додатковий для іншого товару.")
        return codes

    def sync_additional_barcodes(self, product):
        desired = set(self.cleaned_data.get("additional_barcodes") or [])
        existing = {row.barcode: row for row in product.extra_barcodes.all()}
        for code, row in existing.items():
            if code not in desired:
                row.delete()
        for code in desired - set(existing):
            ProductBarcode.objects.create(company=product.company, product=product, barcode=code)

    def clean_photo_url(self):
        value = (self.cleaned_data.get("photo_url") or "").strip()
        if value and not value.lower().startswith("https://"):
            raise forms.ValidationError("Для зовнішнього фото використовуйте безпечне посилання HTTPS.")
        return value

    def clean(self):
        d = super().clean()
        universal_defaults = {
            "product_kind": "goods",
            "tracking_mode": "none",
            "tax_category": "general",
        }
        for field_name, default_value in universal_defaults.items():
            if not d.get(field_name):
                # При редагуванні зберігаємо попереднє значення, якщо старий клієнт
                # не надіслав нове поле; для нового товару беремо стандартне.
                d[field_name] = (
                    getattr(self.instance, field_name) if self.instance.pk else default_value
                )
        condition, defect, description = d.get("condition"), d.get("has_defect"), (d.get("condition_description") or "").strip()
        if condition == "incomplete" and not description:
            self.add_error("condition_description", "Опишіть, яких деталей не вистачає.")
        if condition == "used" and defect and not description:
            self.add_error("condition_description", "Опишіть дефект товару.")
        if condition != "used" and defect:
            self.add_error("has_defect", "Позначка дефекту доступна лише для товару Б/В.")
        if not d.get("category") and d.get("name"):
            text = (d.get("name") or "").casefold()
            rules = [
                (("амортиз", "пружин", "важіл", "важіль", "шарова", "сайлентблок", "ступиц", "підшипник ступ"), "Підвіска та кермування"),
                (("колодк", "диск гальм", "супорт", "гальм", "тормоз"), "Гальмівна система"),
                (("фільтр", "масл", "свіч", "ремінь грм", "ланцюг грм", "порш", "клапан", "прокладк", "двигун"), "Двигун"),
                (("зчеплен", "коробк передач", "кпп", "маховик", "шрус", "привод"), "Трансмісія"),
                (("генератор", "стартер", "датчик", "ламп", "фара", "електр", "котушк"), "Електрика"),
                (("радіатор", "термостат", "помпа", "охолодж", "патруб"), "Охолодження"),
                (("бампер", "крило", "капот", "двер", "дзеркал", "кузов"), "Кузов"),
                (("сидін", "обшив", "ручк двер", "панель", "салон"), "Салон"),
            ]
            category_name = next((name for words, name in rules if any(word in text for word in words)), "")
            if category_name:
                categories = Category.objects.filter(name__iexact=category_name, parent__isnull=True)
                if self.company is not None:
                    categories = categories.filter(company=self.company)
                d["category"] = categories.first()
        brand, sku = d.get("brand"), d.get("sku")
        if sku:
            dup = Product.objects.filter(sku_norm=norm_sku(sku))
            if self.company is not None:
                dup = dup.filter(company=self.company)
                # Автозапчастини ідентифікуються парою бренд + артикул.
                # В інших профілях внутрішній SKU має бути унікальним у межах магазину.
                if self.company.business_type == "auto_parts":
                    dup = dup.filter(brand__iexact=brand or "")
            elif brand:
                dup = dup.filter(brand__iexact=brand)
            if self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            dup = dup.first()
            if dup:
                self.add_error("sku", f"Такий код уже використовується: {dup.brand} {dup.sku} — «{dup.name}». "
                                      f"Коди порівнюються без пробілів і дефісів.")
        return d


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "parent"]

    def __init__(self, *a, company=None, **k):
        super().__init__(*a, **k)
        self.fields["parent"].queryset = Category.objects.filter(company=company) if company else Category.objects.none()


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        exclude = ["company"]

    def __init__(self, *a, company=None, **k):
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
    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company is not None:
            from .models import MarkupTier
            self.fields["markup_tier"].queryset = MarkupTier.objects.filter(company=company)

    def clean_phone(self):
        phone = self.cleaned_data["phone"]
        if self.company is not None and Customer.objects.filter(company=self.company, phone=phone).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Клієнт із таким телефоном уже є в цьому магазині.")
        return phone

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
