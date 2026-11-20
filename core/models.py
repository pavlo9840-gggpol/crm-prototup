import re
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.db.models import Q

USER = settings.AUTH_USER_MODEL
D0 = Decimal("0")


_LAT2CYR = str.maketrans("ABCEHIKMOPTXY", "АВСЕНІКМОРТХУ")


def norm_plate(value):
    """Держномер без пробілів/дефісів, великими літерами, латинські двійники замінено на кириличні."""
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).upper().translate(_LAT2CYR)


def norm_sku(value):
    """Article without spaces/dashes/dots, upper-case: «19-152 615» == «19152615»."""
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).upper()


class Category(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    name = models.CharField("Назва", max_length=100)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="children", verbose_name="Батьківська категорія")

    class Meta:
        ordering = ["name"]
        verbose_name = "Категорія"
        verbose_name_plural = "Категорії"

    def __str__(self):
        return f"{self.parent} → {self.name}" if self.parent_id else self.name


class Supplier(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    name = models.CharField("Назва", max_length=150)
    contact_person = models.CharField("Контактна особа", max_length=100, blank=True)
    phone = models.CharField("Телефон", max_length=30, blank=True)
    notes = models.TextField("Примітки", blank=True)
    is_active = models.BooleanField("Активний", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Постачальник"
        verbose_name_plural = "Постачальники"

    def __str__(self):
        return self.name
    currency = models.ForeignKey("Currency", null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Валюта прайсу",
                                 help_text="У якій валюті постачальник вказує ціни. Порожньо = гривня.")
    fixed_rate = models.DecimalField("Власний курс постачальника", max_digits=12, decimal_places=6, null=True, blank=True,
                                     help_text="Якщо постачальник рахує за своїм курсом: вкажіть його замість курсу НБУ.")
    rate_margin_pct = models.DecimalField("Запас на курс, %", max_digits=5, decimal_places=2, default=0,
                                          help_text="Додається до курсу НБУ (страховка від коливань). До власного курсу не додається.")


class Product(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    name = models.CharField("Назва", max_length=200)
    sku = models.CharField("Артикул", max_length=50, db_index=True)
    barcode = models.CharField("Штрих-код", max_length=50, null=True, blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name="Категорія")
    unit = models.CharField("Одиниця виміру", max_length=20, default="шт")
    PRODUCT_KINDS = [("goods", "Товар"), ("service", "Послуга")]
    product_kind = models.CharField("Тип позиції", max_length=12, choices=PRODUCT_KINDS, default="goods", db_index=True)
    TRACKING_MODES = [("none", "Без додаткового обліку"), ("batch", "Партії"), ("expiry", "Партії та термін придатності"), ("serial", "Серійні номери")]
    tracking_mode = models.CharField("Спосіб обліку", max_length=12, choices=TRACKING_MODES, default="none")
    TAX_CATEGORIES = [("general", "Загальна категорія"), ("vat_standard", "ПДВ — основна ставка"), ("vat_reduced", "ПДВ — знижена ставка"), ("vat_exempt", "Без ПДВ / звільнено"), ("excise", "Підакцизний товар")]
    tax_category = models.CharField("Податкова категорія", max_length=20, choices=TAX_CATEGORIES, default="general")
    weight_kg = models.DecimalField("Вага, кг", max_digits=10, decimal_places=3, null=True, blank=True)
    length_cm = models.DecimalField("Довжина, см", max_digits=10, decimal_places=2, null=True, blank=True)
    width_cm = models.DecimalField("Ширина, см", max_digits=10, decimal_places=2, null=True, blank=True)
    height_cm = models.DecimalField("Висота, см", max_digits=10, decimal_places=2, null=True, blank=True)
    attributes = models.JSONField("Додаткові характеристики", default=dict, blank=True)
    cost_price = models.DecimalField("Ціна закупівлі", max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField("Ціна продажу", max_digits=10, decimal_places=2, default=0)
    allow_price_change = models.BooleanField("Дозволити зміну ціни на касі", default=False)
    current_stock = models.DecimalField("Залишок", max_digits=12, decimal_places=3, default=0)
    min_stock = models.DecimalField("Мінімальний залишок", max_digits=12, decimal_places=3, default=0)
    is_active = models.BooleanField("Активний", default=True)
    is_favorite = models.BooleanField("Швидка кнопка на касі", default=False)
    photo = models.ImageField("Фото", upload_to="products/", blank=True, null=True)
    photo_url = models.URLField("Фото за посиланням", max_length=500, blank=True)
    CONDITION_CHOICES = [("new", "Новий"), ("used", "Б/В"), ("refurbished", "Відновлений"), ("incomplete", "Некомплект")]
    condition = models.CharField("Стан товару", max_length=15, choices=CONDITION_CHOICES, default="new", db_index=True)
    has_defect = models.BooleanField("Є дефект", default=False)
    condition_description = models.TextField("Опис стану", blank=True)
    notes = models.TextField("Примітки", blank=True)
    brand = models.CharField("Бренд (виробник)", max_length=60, blank=True, db_index=True)
    sku_norm = models.CharField(max_length=60, blank=True, db_index=True, editable=False)
    name_lc = models.CharField(max_length=300, blank=True, editable=False)     # назва малими літерами: для пошуку по кирилиці
    from_supplier = models.BooleanField("Створено з прайсу постачальника", default=False, editable=False)
    auto_price = models.BooleanField("Автоціна за правилами", default=False,
                                     help_text="Ціна продажу сама перераховується за правилами цін, коли змінюється закупівельна.")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "barcode"],
                                    condition=Q(company__isnull=False, barcode__isnull=False) & ~Q(barcode=""),
                                    name="uniq_company_product_barcode"),
        ]
        verbose_name = "Товар"
        verbose_name_plural = "Товари"

    def __str__(self):
        return f"{self.name} ({self.sku})"

    def save(self, *a, **k):
        n, lc = norm_sku(self.sku), (self.name or "").lower()
        extra = []
        if self.sku_norm != n:
            self.sku_norm = n
            extra.append("sku_norm")
        if self.name_lc != lc:
            self.name_lc = lc
            extra.append("name_lc")
        if extra and k.get("update_fields") is not None:
            k["update_fields"] = list(k["update_fields"]) + extra
        super().save(*a, **k)

    @property
    def is_low(self):
        return self.current_stock <= self.min_stock



class ProductBarcode(models.Model):
    """Додаткові штрих-коди товару; основний код лишається у Product.barcode для сумісності."""
    company = models.ForeignKey("Company", on_delete=models.CASCADE, related_name="product_barcodes")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="extra_barcodes")
    barcode = models.CharField("Штрих-код", max_length=80)
    label = models.CharField("Позначення", max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]
        constraints = [models.UniqueConstraint(fields=["company", "barcode"], name="uniq_company_extra_barcode")]
        indexes = [models.Index(fields=["product", "barcode"], name="pbarcode_prod_idx")]
        verbose_name = "Додатковий штрих-код"
        verbose_name_plural = "Додаткові штрих-коди"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар належить іншому магазину."})
        self.barcode = (self.barcode or "").strip()

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if self.product_id and not self.company_id:
            self.company_id = self.product.company_id
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({"product": "Товар належить іншому магазину."})
        self.barcode = (self.barcode or "").strip()
        primary_codes = Product.objects.filter(company_id=self.company_id, barcode=self.barcode)
        if self.barcode and primary_codes.exists():
            raise ValidationError({"barcode": "Цей штрих-код уже є основним кодом іншого товару."})
        super().save(*args, **kwargs)


class PriceHistory(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_history")
    field = models.CharField(max_length=10)  # price / cost_price
    old_value = models.DecimalField(max_digits=10, decimal_places=2)
    new_value = models.DecimalField(max_digits=10, decimal_places=2)
    changed_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)
    changed_at = models.DateTimeField(auto_now_add=True)


class Customer(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    name = models.CharField("Ім'я", max_length=100)
    phone = models.CharField("Телефон", max_length=30)
    birthday = models.DateField("День народження", null=True, blank=True)
    notes = models.TextField("Примітки", blank=True)
    created_at = models.DateTimeField("Дата реєстрації", auto_now_add=True)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    purchases_count = models.PositiveIntegerField(default=0)
    markup_tier = models.ForeignKey("MarkupTier", null=True, blank=True, on_delete=models.SET_NULL,
                                    verbose_name="Націнка клієнта")
    name_lc = models.CharField(max_length=200, blank=True, editable=False)

    def save(self, *a, **k):
        lc = (self.name or "").lower()
        if self.name_lc != lc:
            self.name_lc = lc
            if k.get("update_fields") is not None:
                k["update_fields"] = list(k["update_fields"]) + ["name_lc"]
        super().save(*a, **k)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "phone"], condition=Q(company__isnull=False),
                                    name="uniq_company_customer_phone"),
        ]
        verbose_name = "Клієнт"
        verbose_name_plural = "Клієнти"

    def __str__(self):
        return f"{self.name} ({self.phone})"


class Shift(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    cashier = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="shifts")
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    opening_cash = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    opening_counts = models.JSONField(default=dict, blank=True)   # {"kop": count}
    drawer = models.JSONField(default=dict, blank=True)           # tracked cash drawer {"kop": count}
    closing_counted = models.JSONField(null=True, blank=True)
    discrepancy = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["-opened_at"]

    @property
    def is_open(self):
        return self.closed_at is None


class Sale(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    DONE, HELD = "done", "held"
    PAYMENTS = [("cash", "Готівка"), ("card", "Картка"), ("mixed", "Змішана"), ("credit", "У борг")]
    number = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=10, default=DONE, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    cashier = models.ForeignKey(USER, on_delete=models.PROTECT)
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="sales")
    payment_method = models.CharField(max_length=10, choices=PAYMENTS, default="cash")
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    card_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    change_given = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    change_detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def qty_total(self):
        return sum((i.quantity for i in self.items.all()), D0)
    car = models.ForeignKey("CustomerCar", null=True, blank=True, on_delete=models.SET_NULL, related_name="sales")


class SaleItem(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    original_price = models.DecimalField(max_digits=10, decimal_places=2)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    returned_qty = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    # --- товар «під замовлення» від постачальника (минає склад) ---
    STOCK, SUPPLIER = "stock", "supplier"
    ORDERED, ARRIVED, ISSUED, STOCKED, SUP_RETURN, CANCELLED = (
        "ordered", "arrived", "issued", "stocked", "sup_return", "cancelled")
    FULFIL = [(ORDERED, "Замовлено у постачальника"), (ARRIVED, "Прибув, чекає клієнта"),
              (ISSUED, "Видано клієнту"), (STOCKED, "Оприбутковано на склад"),
              (SUP_RETURN, "Повернуто постачальнику"), (CANCELLED, "Скасовано")]
    source = models.CharField(max_length=10, default=STOCK, db_index=True)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    supplier_item = models.ForeignKey("SupplierItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    fulfil_status = models.CharField(max_length=12, blank=True, default="", db_index=True)
    markup_pct = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    pickup_deadline = models.DateField(null=True, blank=True)
    receipt_ref = models.CharField(max_length=60, blank=True)          # номер накладної, за якою товар прийшов
    earned_at = models.DateTimeField(null=True, blank=True, db_index=True)   # коли маржа вважається зароблена (для зарплати)

    @property
    def line_total(self):
        return self.quantity * self.price

    @property
    def margin(self):
        return (self.quantity - self.returned_qty) * (self.price - self.cost_price)

    @property
    def is_pending(self):
        return self.source == self.SUPPLIER and self.fulfil_status in (self.ORDERED, self.ARRIVED)

    @property
    def days_left(self):
        """Днів до кінця відліку (від'ємне — прострочено); None, якщо відлік не запущено."""
        if self.fulfil_status != self.ARRIVED or not self.pickup_deadline:
            return None
        from django.utils import timezone
        return (self.pickup_deadline - timezone.localdate()).days

    @property
    def returnable(self):
        return self.quantity - self.returned_qty


class SaleReturn(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    REFUND_METHODS = [
        ("pending", "Очікує виплати"), ("cash", "Готівка"),
        ("card", "Картка"), ("mixed", "Змішана"),
    ]
    number = models.CharField(max_length=20, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refund_method = models.CharField(max_length=10, choices=REFUND_METHODS, default="pending")
    cash_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    card_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shift = models.ForeignKey("Shift", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="sale_returns")


class SaleReturnItem(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    price = models.DecimalField(max_digits=10, decimal_places=2)


class Purchase(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    number = models.CharField(max_length=20, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=15, default="Підтверджено")

    class Meta:
        ordering = ["-created_at"]


class PurchaseItem(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)
    order_item = models.ForeignKey("PurchaseOrderItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="receipt_items")

    @property
    def line_total(self):
        return self.quantity * self.cost_price


class PurchaseOrder(models.Model):
    DRAFT = "draft"
    SENT = "sent"
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    RECEIVED = "received"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Чернетка"), (SENT, "Відправлено"), (CONFIRMED, "Підтверджено"),
        (PARTIAL, "Частково отримано"), (RECEIVED, "Отримано"), (CANCELLED, "Скасовано"),
    ]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="purchase_orders")
    number = models.CharField("Номер замовлення", max_length=24, blank=True, db_index=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField("Статус", max_length=12, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expected_date = models.DateField("Очікувана дата", null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="created_purchase_orders")
    note = models.TextField("Примітка", blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status", "created_at"], name="po_company_status_idx"),
            models.Index(fields=["company", "supplier", "status"], name="po_company_supplier_idx"),
        ]

    def __str__(self):
        return self.number or f"Замовлення #{self.pk}"


class PurchaseOrderItem(models.Model):
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="purchase_order_items")
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="purchase_order_items")
    supplier_item = models.ForeignKey("SupplierItem", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="purchase_order_items")
    quantity = models.DecimalField("Замовлено", max_digits=12, decimal_places=3)
    received_quantity = models.DecimalField("Отримано", max_digits=12, decimal_places=3, default=0)
    cost_price = models.DecimalField("Ціна замовлення", max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["product__name", "pk"]
        indexes = [models.Index(fields=["company", "product"], name="poi_company_product_idx")]

    @property
    def outstanding_quantity(self):
        return max(Decimal("0"), self.quantity - self.received_quantity)

    @property
    def line_total(self):
        return self.quantity * self.cost_price


class SupplierClaim(models.Model):
    """Претензія до постачальника, пов'язана з конкретним рядком замовлення."""
    OPEN, SENT, ACCEPTED, REJECTED, RESOLVED, CANCELLED = (
        "open", "sent", "accepted", "rejected", "resolved", "cancelled"
    )
    STATUS_CHOICES = [
        (OPEN, "Чернетка"), (SENT, "Відправлено"), (ACCEPTED, "Прийнято постачальником"),
        (REJECTED, "Відхилено постачальником"), (RESOLVED, "Вирішено"),
        (CANCELLED, "Скасовано"),
    ]
    KIND_CHOICES = [
        ("shortage", "Недопоставка"), ("damaged", "Пошкодження"),
        ("wrong_item", "Не той товар"), ("quality", "Якість товару"),
        ("price", "Невідповідність ціни"), ("late", "Запізнення доставки"),
        ("other", "Інше"),
    ]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="supplier_claims")
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="claims")
    order_item = models.ForeignKey(PurchaseOrderItem, on_delete=models.PROTECT, related_name="claims")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="claims")
    claim_number = models.CharField(max_length=24, blank=True, db_index=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    requested_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    description = models.TextField()
    supplier_response = models.TextField(blank=True)
    resolution_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="supplier_claims_created")
    resolved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="supplier_claims_resolved")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["company", "status", "created_at"], name="claim_company_status_idx"),
            models.Index(fields=["company", "supplier", "created_at"], name="claim_company_supplier_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__isnull=True) | Q(quantity__gt=0), name="supplier_claim_qty_positive"),
            models.CheckConstraint(condition=Q(requested_amount__gte=0), name="supplier_claim_amount_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        if (self.order.company_id != self.company_id or self.order_item.company_id != self.company_id
                or self.order_item.order_id != self.order_id or self.supplier_id != self.order.supplier_id):
            raise ValueError("Претензія має посилатися на замовлення, рядок і постачальника однієї компанії.")
        super().save(*args, **kwargs)
        if not self.claim_number:
            self.claim_number = f"CLM-{self.pk:06d}"
            super().save(update_fields=["claim_number", "updated_at"])

    def __str__(self):
        return self.claim_number or f"Претензія #{self.pk}"


class SupplierReturn(models.Model):
    """Фактичне повернення отриманого товару постачальнику; створюється лише разом зі списанням зі складу."""
    COMPENSATION_CHOICES = [
        ("credit", "Зарахувати в рахунок боргу"),
        ("refund", "Повернення коштів"),
        ("replacement", "Заміна товару"),
    ]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="supplier_returns")
    purchase_item = models.ForeignKey(PurchaseItem, on_delete=models.PROTECT, related_name="supplier_returns")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="returns")
    number = models.CharField("Номер повернення", max_length=24, blank=True, db_index=True)
    quantity = models.DecimalField("Кількість", max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField("Собівартість одиниці", max_digits=12, decimal_places=2)
    compensation_type = models.CharField("Очікувана компенсація", max_length=12, choices=COMPENSATION_CHOICES, default="credit")
    reason = models.TextField("Причина повернення")
    note = models.TextField("Примітка", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField()
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="supplier_returns_created")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["company", "created_at"], name="sup_return_company_date_idx"),
            models.Index(fields=["company", "supplier"], name="sup_return_company_sup_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="supplier_return_qty_positive"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="supplier_return_cost_nonnegative"),
        ]

    @property
    def total(self):
        return self.quantity * self.unit_cost

    def save(self, *args, **kwargs):
        purchase = self.purchase_item.purchase
        if (self.purchase_item.company_id != self.company_id
                or purchase.company_id != self.company_id
                or purchase.supplier_id != self.supplier_id
                or self.supplier.company_id != self.company_id
                or self.purchase_item.product.company_id != self.company_id):
            raise ValueError("Повернення має посилатися на закупівлю, товар і постачальника одного магазину.")
        super().save(*args, **kwargs)
        if not self.number:
            self.number = f"SR-{self.pk:06d}"
            super().save(update_fields=["number"])


class SupplierCompensation(models.Model):
    """Confirmed supplier compensation: credit note, cash refund or bank refund."""
    CREDIT, CASH, BANK = "credit", "cash", "bank"
    KINDS = [
        (CREDIT, "Кредит-нота / залік боргу"),
        (CASH, "Повернення готівкою"),
        (BANK, "Повернення на банківський рахунок"),
    ]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="supplier_compensations")
    supplier_return = models.ForeignKey(SupplierReturn, on_delete=models.PROTECT, related_name="compensations")
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="supplier_compensations")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="compensations")
    number = models.CharField("Номер компенсації", max_length=24, blank=True, db_index=True)
    kind = models.CharField("Тип компенсації", max_length=10, choices=KINDS)
    amount = models.DecimalField("Сума", max_digits=14, decimal_places=2)
    bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="supplier_compensations")
    cash_operation = models.OneToOneField("CashOperation", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="supplier_compensation")
    bank_transaction = models.OneToOneField("BankTransaction", null=True, blank=True, on_delete=models.PROTECT,
                                            related_name="supplier_compensation")
    reversal_cash_operation = models.OneToOneField("CashOperation", null=True, blank=True, on_delete=models.PROTECT,
                                                   related_name="supplier_compensation_reversal")
    reversal_bank_transaction = models.OneToOneField("BankTransaction", null=True, blank=True, on_delete=models.PROTECT,
                                                     related_name="supplier_compensation_reversal")
    note = models.CharField("Примітка", max_length=250, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="supplier_compensations_created")
    created_at = models.DateTimeField(auto_now_add=True)
    is_void = models.BooleanField("Скасовано", default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="supplier_compensations_voided")
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["company", "created_at"], name="sup_comp_company_date_idx"),
            models.Index(fields=["company", "supplier"], name="sup_comp_company_sup_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="supplier_comp_amount_positive"),
        ]

    def save(self, *args, **kwargs):
        returned = self.supplier_return
        if (returned.company_id != self.company_id
                or returned.supplier_id != self.supplier_id
                or returned.purchase_item.purchase_id != self.purchase_id
                or self.purchase.company_id != self.company_id
                or self.supplier.company_id != self.company_id):
            raise ValueError("Компенсація має бути прив'язана до повернення, закупівлі й постачальника одного магазину.")
        if self.kind not in {self.CREDIT, self.CASH, self.BANK}:
            raise ValueError("Невідомий спосіб компенсації постачальника.")
        if self.kind == self.CASH:
            if not self.cash_operation_id or self.bank_transaction_id or self.bank_account_id:
                raise ValueError("Готівкова компенсація повинна мати лише касову операцію.")
            if (self.cash_operation.company_id != self.company_id
                    or self.cash_operation.shift.company_id != self.company_id):
                raise ValueError("Касова операція компенсації належить іншому магазину.")
        if self.kind == self.BANK:
            if not self.bank_transaction_id or not self.bank_account_id or self.cash_operation_id:
                raise ValueError("Банківська компенсація повинна мати банківську операцію та рахунок.")
            if (self.bank_account.company_id != self.company_id
                    or self.bank_transaction.company_id != self.company_id
                    or self.bank_transaction.account_id != self.bank_account_id):
                raise ValueError("Банківська операція компенсації має бути на рахунку цього магазину.")
        if self.kind == self.CREDIT and (self.cash_operation_id or self.bank_transaction_id or self.bank_account_id):
            raise ValueError("Кредит-нота не повинна створювати рух грошей.")
        super().save(*args, **kwargs)
        if not self.number:
            self.number = f"SC-{self.pk:06d}"
            super().save(update_fields=["number"])


class StockBatch(models.Model):
    """Окрема партія надходження з власною собівартістю та залишком."""
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="stock_batches")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_batches")
    purchase_item = models.ForeignKey(PurchaseItem, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="batches")
    batch_number = models.CharField("Номер партії", max_length=80, blank=True)
    expiry_date = models.DateField("Термін придатності", null=True, blank=True, db_index=True)
    received_quantity = models.DecimalField("Отримано", max_digits=12, decimal_places=3)
    remaining_quantity = models.DecimalField("Залишок партії", max_digits=12, decimal_places=3)
    cost_price = models.DecimalField("Собівартість одиниці", max_digits=12, decimal_places=2)
    received_at = models.DateTimeField("Дата надходження", auto_now_add=True)

    class Meta:
        ordering = ["expiry_date", "received_at", "pk"]
        indexes = [models.Index(fields=["company", "product", "expiry_date"], name="batch_stock_exp_idx")]

    def __str__(self):
        return f"{self.product} / {self.batch_number or self.pk}"


class StockSerialUnit(models.Model):
    """Окремий фізичний екземпляр товару з унікальним серійним номером / IMEI."""
    IN_STOCK, SOLD = "in_stock", "sold"
    STATUS_CHOICES = [(IN_STOCK, "На складі"), (SOLD, "Продано")]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="stock_serial_units")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="serial_units")
    purchase_item = models.ForeignKey("PurchaseItem", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="serial_units")
    sale_item = models.ForeignKey("SaleItem", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="serial_units")
    serial_number = models.CharField("Серійний номер / IMEI", max_length=100)
    normalized_serial = models.CharField(max_length=100, editable=False)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=IN_STOCK, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True)
    sold_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["product__name", "serial_number"]
        constraints = [
            models.UniqueConstraint(fields=["company", "normalized_serial"], name="uniq_company_serial_number"),
        ]
        indexes = [models.Index(fields=["company", "product", "status"], name="serial_stock_status_idx")]

    def save(self, *args, **kwargs):
        self.serial_number = (self.serial_number or "").strip()
        self.normalized_serial = "".join(ch for ch in self.serial_number if ch.isalnum()).upper()
        if not self.normalized_serial:
            raise ValueError("Серійний номер не може бути порожнім")
        if len(self.serial_number) > 100 or len(self.normalized_serial) > 100:
            raise ValueError("Серійний номер має містити не більше 100 символів")
        if not self.company_id or not self.product_id:
            raise ValueError("Для серійного номера обов'язково вкажіть магазин і товар")
        product_company = Product.objects.filter(pk=self.product_id).values_list("company_id", flat=True).first()
        if product_company != self.company_id:
            raise ValueError("Серійний номер і товар мають належати одному магазину")
        if self.purchase_item_id:
            purchase_item = PurchaseItem.objects.filter(pk=self.purchase_item_id).values("company_id", "product_id").first()
            if not purchase_item or purchase_item["company_id"] != self.company_id or purchase_item["product_id"] != self.product_id:
                raise ValueError("Надходження серійного номера не відповідає магазину або товару")
        if self.sale_item_id:
            sale_item = SaleItem.objects.filter(pk=self.sale_item_id).values("company_id", "product_id").first()
            if not sale_item or sale_item["company_id"] != self.company_id or sale_item["product_id"] != self.product_id:
                raise ValueError("Продаж серійного номера не відповідає магазину або товару")
        super().save(*args, **kwargs)


class BatchAllocation(models.Model):
    """Фіксує, з яких партій списано позицію продажу; повернення відновлює саме їх."""
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="batch_allocations")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.CASCADE, related_name="batch_allocations")
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT, related_name="allocations")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    returned_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["sale_item", "batch"], name="uniq_sale_item_batch")]


class StockAdjustment(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    KINDS = [("writeoff", "Списання"), ("surplus", "Надлишок"), ("shortage", "Нестача")]
    REASONS = [("damaged", "Пошкодження"), ("expired", "Прострочення"), ("theft", "Крадіжка"), ("lost", "Втрата"), ("internal", "Внутрішнє використання"), ("other", "Інше")]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="Товар")
    kind = models.CharField("Тип", max_length=10, choices=KINDS)
    quantity = models.DecimalField("Кількість", max_digits=10, decimal_places=3)
    reason = models.CharField("Причина", max_length=10, choices=REASONS, blank=True)
    note = models.TextField("Примітка", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT)



class InventoryCount(models.Model):
    """Знімок залишків на момент початку інвентаризації."""
    DRAFT, APPROVED = "draft", "approved"
    STATUS_CHOICES = [(DRAFT, "Чернетка"), (APPROVED, "Затверджено")]
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="inventory_counts")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    note = models.TextField("Примітка", blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="created_inventory_counts")
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_inventory_counts")
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    @property
    def number(self):
        return f"INV-{self.pk:06d}"


class InventoryCountItem(models.Model):
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="+")
    inventory_count = models.ForeignKey(InventoryCount, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="inventory_count_items")
    expected_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    counted_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    book_quantity_at_approval = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    counted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["product__name", "product__sku"]
        constraints = [
            models.UniqueConstraint(fields=["inventory_count", "product"], name="uniq_inventory_count_product"),
        ]
        indexes = [models.Index(fields=["company", "inventory_count"], name="inv_count_item_company_idx")]

    @property
    def difference(self):
        if self.counted_quantity is None:
            return None
        baseline = self.book_quantity_at_approval if self.book_quantity_at_approval is not None else self.expected_quantity
        return self.counted_quantity - baseline


class StockMovement(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    TYPES = [
        ("purchase", "Надходження"), ("supplier_replacement", "Заміна від постачальника"), ("sale", "Видаток продажу"),
        ("customer_return", "Повернення від клієнта"), ("supplier_return", "Повернення постачальнику"),
        ("writeoff", "Списання"), ("surplus", "Надлишок"), ("shortage", "Нестача"),
        ("opening", "Початковий залишок"), ("transit_stock", "Оприбутковано: клієнт не забрав"),
    ]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=TYPES, db_index=True)
    quantity_change = models.DecimalField(max_digits=12, decimal_places=3)
    balance_after = models.DecimalField(max_digits=12, decimal_places=3)
    doc_ref = models.CharField(max_length=30, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at", "-id"]


class CashOperation(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    KINDS = [("in", "Внесення"), ("out", "Вилучення / інкасація"), ("adjust", "Корекція")]
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="cash_ops")
    user = models.ForeignKey(USER, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=10, choices=KINDS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    detail = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]


class CompanySettings(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    name = models.CharField("Назва магазину", max_length=150, default="Мій магазин")
    legal_name = models.CharField("Юридична назва", max_length=200, blank=True)
    tax_id = models.CharField("ЄДРПОУ / ІПН", max_length=30, blank=True)
    vat_id = models.CharField("Номер платника ПДВ", max_length=30, blank=True)
    address = models.CharField("Адреса", max_length=250, blank=True)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    email = models.CharField("Email", max_length=100, blank=True)
    website = models.CharField("Сайт", max_length=100, blank=True)
    logo = models.ImageField("Логотип", upload_to="company/", blank=True, null=True)
    receipt_header = models.TextField("Текст зверху чека", blank=True)
    receipt_footer = models.TextField("Текст внизу чека", blank=True, default="Дякуємо за покупку!")
    auto_print = models.BooleanField("Друкувати чек автоматично після продажу", default=False)
    auto_print_format = models.CharField("Формат автодруку чека", max_length=5, default="html",
                                         choices=[("html", "Браузерний друк (HTML)"), ("pdf", "Відкрити PDF")])
    vat_rate = models.DecimalField("Ставка ПДВ, % (0 — без ПДВ)", max_digits=5, decimal_places=2, default=0)
    pickup_days = models.PositiveIntegerField("Днів на забір товару клієнтом (відлік)", default=14)

    class Meta:
        verbose_name = "Налаштування фірми"
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(company__isnull=False),
                name="uniq_company_settings",
            ),
        ]

    @classmethod
    def get(cls, company=None):
        if company is not None:
            obj, _ = cls.objects.get_or_create(company=company, defaults={"name": company.name})
        else:
            obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BankAccount(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    bank_name = models.CharField("Банк", max_length=100)
    iban = models.CharField("IBAN / рахунок", max_length=40)
    mfo = models.CharField("МФО", max_length=15, blank=True)
    holder = models.CharField("Отримувач", max_length=150, blank=True)
    currency = models.CharField("Валюта", max_length=5, default="UAH")
    note = models.CharField("Примітка", max_length=150, blank=True)
    is_default = models.BooleanField("Основний", default=False)

    class Meta:
        ordering = ["-is_default", "id"]


class DocTemplate(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """LEGACY (v2 HTML templates). Kept so existing data is not lost; replaced by PrintTemplate."""
    kind = models.CharField(max_length=20)
    body = models.TextField()
    paper = models.CharField(max_length=10, default="80mm")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "kind"], condition=Q(company__isnull=False),
                                    name="uniq_company_legacy_doc_kind"),
        ]


class PrintTemplate(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Print form designed in the visual designer. layout = JSON (see core/designer.py)."""
    doc_type = models.CharField(max_length=20, db_index=True)
    name = models.CharField(max_length=150)
    layout = models.JSONField(default=dict)
    is_builtin = models.BooleanField(default=False)
    builtin_key = models.CharField(max_length=40, blank=True, db_index=True)
    is_default = models.BooleanField(default=False)
    format_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["doc_type", "-is_builtin", "name"]

    def __str__(self):
        return f"{self.doc_type}: {self.name}"


# ====================================================== прайси постачальників / аналоги / націнки
class SupplierItem(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Рядок прайсу постачальника. Не є складським товаром: залишок складу не змінює."""
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="items")
    brand = models.CharField("Бренд", max_length=60, blank=True)
    sku = models.CharField("Артикул", max_length=80)
    sku_norm = models.CharField(max_length=80, db_index=True)
    name = models.CharField("Назва", max_length=250, blank=True)
    cost = models.DecimalField("Закупівельна ціна", max_digits=12, decimal_places=2, default=0)
    qty_avail = models.DecimalField("Наявність у постачальника", max_digits=12, decimal_places=3, null=True, blank=True)
    available = models.BooleanField("Є в прайсі", default=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="supplier_items")
    ext_id = models.CharField("ID у постачальника", max_length=64, blank=True, db_index=True)
    name_lc = models.CharField(max_length=300, blank=True, editable=False)
    price_orig = models.DecimalField("Ціна у валюті постачальника", max_digits=14, decimal_places=4, null=True, blank=True)

    def save(self, *a, **k):
        lc = (self.name or "").lower()
        if self.name_lc != lc:
            self.name_lc = lc
            if k.get("update_fields") is not None:
                k["update_fields"] = list(k["update_fields"]) + ["name_lc"]
        super().save(*a, **k)

    class Meta:
        unique_together = [("supplier", "brand", "sku_norm")]
        verbose_name = "Позиція постачальника"
        verbose_name_plural = "Прайс постачальників"

    def __str__(self):
        return f"{self.brand} {self.sku} — {self.supplier}"


class PriceImport(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="imports")
    user = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    file_name = models.CharField(max_length=200, blank=True)
    n_new = models.PositiveIntegerField(default=0)
    n_updated = models.PositiveIntegerField(default=0)
    n_gone = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]


class CrossRef(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Пара аналогів (за нормалізованим артикулом), симетрична."""
    a = models.CharField(max_length=80, db_index=True)
    b = models.CharField(max_length=80, db_index=True)
    a_raw = models.CharField(max_length=80, blank=True)
    b_raw = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=10, default="import")        # manual — підтверджено людиною

    class Meta:
        unique_together = [("a", "b")]


class MarkupTier(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    name = models.CharField("Назва", max_length=60)
    percent = models.DecimalField("Націнка, %", max_digits=6, decimal_places=2, default=30)
    is_default = models.BooleanField("За замовчуванням (для клієнтів без категорії)", default=False)

    class Meta:
        ordering = ["percent"]
        verbose_name = "Націнка"
        verbose_name_plural = "Націнки"

    def __str__(self):
        return f"{self.name} (+{self.percent.normalize():f}%)"


class SupplierApi(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Підключення до API / фіду постачальника: каталог підтягується у «Прайси постачальників»."""
    AUTH = [("none", "Без ключа"), ("bearer", "Заголовок Authorization: Bearer <ключ>"),
            ("header", "Власний заголовок (напр. X-API-Key)"), ("query", "Параметр у посиланні (напр. ?api_key=…)")]
    FMT = [("json", "JSON"), ("xml", "XML (у т.ч. YML-фіди)"), ("csv", "Таблиця за посиланням (CSV / XLSX)")]
    supplier = models.OneToOneField(Supplier, on_delete=models.CASCADE, related_name="api")
    url = models.CharField("Посилання (URL) API або фіду", max_length=500)
    fmt = models.CharField("Формат відповіді", max_length=5, choices=FMT, default="json")
    auth_mode = models.CharField("Як передавати ключ", max_length=8, choices=AUTH, default="bearer")
    auth_name = models.CharField("Назва заголовка / параметра", max_length=60, blank=True)
    api_key = models.CharField(max_length=300, blank=True)
    items_path = models.CharField("Де лежить список товарів", max_length=120, blank=True,
                                  help_text="JSON: шлях через крапку, напр. data.items (порожньо, якщо відповідь і є списком). "
                                            "XML: назва тегу однієї позиції, напр. offer.")
    f_sku = models.CharField("Поле: артикул *", max_length=80)
    f_brand = models.CharField("Поле: бренд", max_length=80, blank=True)
    f_name = models.CharField("Поле: назва", max_length=80, blank=True)
    f_cost = models.CharField("Поле: закупівельна ціна *", max_length=80)
    f_qty = models.CharField("Поле: залишок / наявність", max_length=80, blank=True)
    page_param = models.CharField("Параметр номера сторінки", max_length=30, blank=True,
                                  help_text="Якщо API віддає каталог частинами (напр. page): програма сама піде по сторінках.")
    max_pages = models.PositiveIntegerField("Максимум сторінок", default=500)
    method = models.CharField("Метод запиту", max_length=4, choices=[("GET", "GET"), ("POST", "POST")], default="GET")
    body = models.TextField("Тіло запиту (JSON, для POST)", blank=True)
    user_agent = models.CharField("User-Agent", max_length=80, default="RetailShop/1.0",
                                  help_text="Деякі API (напр. BM Parts) відхиляють запити без User-Agent.")
    header_row = models.PositiveSmallIntegerField("Рядок з назвами колонок (для таблиць)", default=1,
                                                  help_text="У прайсі BM Parts назви для програм у 2-му рядку.")
    f_id = models.CharField("Поле: ID товару у постачальника", max_length=80, blank=True,
                            help_text="Потрібне, щоб підтягувати OE-коди й описи з картки товару.")
    # дані про деталь (OE-коди, опис, характеристики) з картки товару
    det_url = models.CharField("Посилання на картку товару", max_length=500, blank=True,
                               help_text="Де є {id}, напр. https://api.bm.parts/product/{id}?oe=full")
    det_oe = models.CharField("Картка: де лежать OE-коди", max_length=120, blank=True, help_text="Шлях до списку; порожньо = автовизначення.")
    det_oe_code = models.CharField("Картка: поле коду в OE-записі", max_length=60, blank=True)
    det_oe_brand = models.CharField("Картка: поле бренду в OE-записі", max_length=60, blank=True)
    det_desc = models.CharField("Картка: де лежить опис", max_length=120, blank=True)
    det_attrs = models.CharField("Картка: де лежать характеристики", max_length=120, blank=True)
    det_cars = models.CharField("Картка: де лежить застосовність до авто", max_length=120, blank=True)
    det_analogs = models.CharField("Картка: де лежать аналоги (кроси)", max_length=120, blank=True,
                                   help_text="Список артикулів або обʼєктів із брендом і артикулом; порожньо = автовизначення.")
    det_image = models.CharField("Картка: де лежить фото", max_length=120, blank=True)
    det_image_base = models.CharField("Початок посилання на фото", max_length=200, blank=True,
                                      help_text="Якщо в картці лише шлях до фото, сюди вписують адресу CDN.")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "API постачальника"


# ====================================================== дані про запчастини (аналог TecDoc)
class PartOe(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Звʼязок «деталь ↔ OE-номер». Деталі зі спільним OE-номером вважаються точними аналогами."""
    brand = models.CharField(max_length=60, db_index=True)
    sku_norm = models.CharField(max_length=80)
    sku = models.CharField(max_length=80, blank=True)
    oe_norm = models.CharField(max_length=80, db_index=True)
    oe_raw = models.CharField(max_length=80)
    oe_brand = models.CharField("Бренд автовиробника", max_length=60, blank=True)
    source = models.CharField(max_length=20, default="import")        # api / import / manual

    class Meta:
        unique_together = [("brand", "sku_norm", "oe_norm")]
        indexes = [models.Index(fields=["brand", "sku_norm"])]


class PartInfo(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Опис, характеристики, застосовність і фото деталі (ключ — бренд + артикул)."""
    brand = models.CharField(max_length=60)
    sku_norm = models.CharField(max_length=80)
    sku = models.CharField(max_length=80, blank=True)
    description = models.TextField("Опис", blank=True)
    attrs = models.TextField("Характеристики", blank=True)             # рядки «назва: значення»
    cars = models.TextField("Застосовність до авто", blank=True)       # по рядку на авто
    image_url = models.CharField(max_length=400, blank=True)
    source = models.CharField(max_length=20, default="manual")
    updated_at = models.DateTimeField(auto_now=True)
    cars_lc = models.TextField(blank=True, editable=False)           # застосовність малими літерами: пошук по кирилиці

    class Meta:
        unique_together = [("brand", "sku_norm")]

    def save(self, *a, **k):
        lc = (self.cars or "").lower()
        if self.cars_lc != lc:
            self.cars_lc = lc
            if k.get("update_fields") is not None:
                k["update_fields"] = list(k["update_fields"]) + ["cars_lc"]
        super().save(*a, **k)

    @property
    def attr_pairs(self):
        out = []
        for line in self.attrs.splitlines():
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                out.append((k.strip(), v.strip()))
        return out


class CrossReject(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Пара, яку людина визнала НЕ аналогами: приховується з усіх автоматичних джерел."""
    a = models.CharField(max_length=80)
    b = models.CharField(max_length=80)
    user = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("a", "b")]


class CustomerCar(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Авто клієнта: за ним підбираються запчастини й шукається клієнт на касі."""
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="cars")
    plate = models.CharField("Держномер", max_length=20, blank=True)
    plate_norm = models.CharField(max_length=20, blank=True, db_index=True, editable=False)
    vin = models.CharField("VIN", max_length=17, blank=True, db_index=True)
    make = models.CharField("Марка", max_length=40, blank=True)
    model = models.CharField("Модель", max_length=60, blank=True)
    year = models.PositiveSmallIntegerField("Рік випуску", null=True, blank=True)
    engine = models.CharField("Двигун / обʼєм", max_length=60, blank=True)
    note = models.CharField("Примітка", max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Авто клієнта"

    def save(self, *a, **k):
        self.plate_norm = norm_plate(self.plate)
        self.vin = self.vin.strip().upper()
        if k.get("update_fields") is not None:
            k["update_fields"] = list(k["update_fields"]) + ["plate_norm", "vin"]
        super().save(*a, **k)

    @property
    def title(self):
        base = " ".join(x for x in (self.make, self.model, str(self.year or "")) if x).strip()
        return f"{self.plate or 'без номера'} · {base}" if base else (self.plate or self.vin or "авто")


class Receipt(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Журнал приймання накладної постачальника (хто, коли, скільки пішло під клієнтів і скільки на склад)."""
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="receipts")
    number = models.CharField("Номер накладної", max_length=60, blank=True)
    user = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    n_matched = models.PositiveIntegerField(default=0)       # позицій чеків клієнтів, які прибули
    n_stock = models.PositiveIntegerField(default=0)         # рядків, оприбуткованих на склад
    n_skipped = models.PositiveIntegerField(default=0)
    purchase = models.ForeignKey(Purchase, null=True, blank=True, on_delete=models.SET_NULL)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


# ====================================================== правила цін, зарплата
class PriceRule(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Правило ціни: націнка, мінімальна маржа й округлення для бренду та/або категорії.
    Найконкретніше правило перемагає: бренд + категорія, потім бренд, потім категорія, потім загальне."""
    ROUND = [(0, "без округлення"), (1, "до 1 ₴ вгору"), (5, "до 5 ₴ вгору"), (10, "до 10 ₴ вгору")]
    name = models.CharField("Назва", max_length=80)
    brand = models.CharField("Бренд (порожньо = будь-який)", max_length=60, blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.CASCADE, verbose_name="Категорія (порожньо = будь-яка)")
    markup_pct = models.DecimalField("Націнка, %", max_digits=6, decimal_places=2, null=True, blank=True,
                                     help_text="Порожньо: береться націнка клієнта або типова.")
    min_margin_pct = models.DecimalField("Мінімальна маржа, % від ціни", max_digits=5, decimal_places=2, default=0,
                                         help_text="Ціна ніколи не буде нижчою, ніж потрібно для цієї маржі.")
    rounding = models.PositiveSmallIntegerField("Округлення", choices=ROUND, default=0)
    priority = models.PositiveIntegerField("Пріоритет (менше = вище)", default=100)
    is_active = models.BooleanField("Діє", default=True)

    class Meta:
        ordering = ["priority", "pk"]
        verbose_name = "Правило ціни"

    def __str__(self):
        scope = " · ".join(x for x in (self.brand, str(self.category) if self.category_id else "") if x) or "усі товари"
        return f"{self.name} ({scope})"


class SupplierPriceChange(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Журнал змін закупівельної ціни в прайсах постачальників."""
    item = models.ForeignKey(SupplierItem, on_delete=models.CASCADE, related_name="changes")
    old_cost = models.DecimalField(max_digits=12, decimal_places=2)
    new_cost = models.DecimalField(max_digits=12, decimal_places=2)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at", "-pk"]


class PayPlan(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """План оплати менеджера: відсоток від маржі + фіксована частина. Запис без менеджера = типовий план."""
    user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.CASCADE, related_name="pay_plans",
                             verbose_name="Менеджер (порожньо = типовий план для всіх)")
    margin_pct = models.DecimalField("Відсоток від маржі, %", max_digits=5, decimal_places=2, default=10)
    fixed_monthly = models.DecimalField("Фіксована частина на місяць, ₴", max_digits=10, decimal_places=2, default=0)
    note = models.CharField("Примітка", max_length=200, blank=True)

    class Meta:
        verbose_name = "План оплати"
        constraints = [
            models.UniqueConstraint(fields=["company", "user"],
                                    condition=Q(company__isnull=False, user__isnull=False),
                                    name="uniq_company_payplan_user"),
            models.UniqueConstraint(fields=["company"], condition=Q(company__isnull=False, user__isnull=True),
                                    name="uniq_company_payplan_default"),
        ]


# ====================================================== валюти й курси
class Currency(models.Model):
    code = models.CharField("Код", max_length=3, unique=True)
    name = models.CharField("Назва", max_length=40)
    symbol = models.CharField("Символ", max_length=4, blank=True)
    is_base = models.BooleanField("Національна (гривня)", default=False)

    class Meta:
        ordering = ["-is_base", "code"]
        verbose_name = "Валюта"
        verbose_name_plural = "Валюти"

    def __str__(self):
        return self.code


class ExchangeRate(models.Model):
    """Курс: скільки гривень за 1 одиницю валюти на дату."""
    currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name="rates")
    date = models.DateField(db_index=True)
    rate = models.DecimalField(max_digits=12, decimal_places=6)
    source = models.CharField(max_length=10, default="manual")        # nbu / manual
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("currency", "date")]
        ordering = ["-date", "currency__code"]


# ---------------------------------------------------------------------------
# Normalized spare-parts catalogue. Catalogue identities are separate from
# stock Products and supplier offers; importing a catalogue must never create
# stock or alter prices.
class CatalogSource(models.Model):
    name = models.CharField(max_length=120, unique=True)
    url = models.URLField(max_length=500, blank=True)
    license_name = models.CharField(max_length=120, blank=True)
    attribution = models.CharField(max_length=250, blank=True)
    version = models.CharField(max_length=80, blank=True)
    commercial_use = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class CatalogImportRun(models.Model):
    source = models.ForeignKey(CatalogSource, on_delete=models.PROTECT, related_name="runs")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, default="running",
                              choices=[("running", "Виконується"), ("success", "Успішно"), ("failed", "Помилка")])
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True)


class VehicleMake(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140)
    kind = models.CharField(max_length=20, default="car", db_index=True)
    source = models.ForeignKey(CatalogSource, on_delete=models.PROTECT, related_name="vehicle_makes")
    external_id = models.CharField(max_length=180, blank=True)
    name_norm = models.CharField(max_length=140, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "slug"], name="uniq_vehicle_make_kind_slug"),
            models.UniqueConstraint(fields=["source", "external_id"], condition=~models.Q(external_id=""), name="uniq_vehicle_make_source_id"),
        ]
        ordering = ["name", "kind"]

    def __str__(self):
        return self.name


class VehicleModel(models.Model):
    make = models.ForeignKey(VehicleMake, on_delete=models.PROTECT, related_name="models")
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180)
    source_id = models.CharField(max_length=220, blank=True)
    body_types = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["make", "slug"], name="uniq_vehicle_model_make_slug"),
            models.UniqueConstraint(fields=["make", "source_id"], condition=~models.Q(source_id=""), name="uniq_vehicle_model_source_id"),
        ]
        ordering = ["name"]

    def __str__(self):
        return f"{self.make.name} {self.name}"


class VehicleVariant(models.Model):
    """A more specific fitment target; the open base catalogue may only know nameplates."""
    model = models.ForeignKey(VehicleModel, on_delete=models.PROTECT, related_name="variants")
    label = models.CharField(max_length=180)
    year_from = models.PositiveSmallIntegerField(null=True, blank=True)
    year_to = models.PositiveSmallIntegerField(null=True, blank=True)
    engine_code = models.CharField(max_length=80, blank=True)
    engine_description = models.CharField(max_length=160, blank=True)
    body_code = models.CharField(max_length=80, blank=True)
    source = models.ForeignKey(CatalogSource, on_delete=models.PROTECT, related_name="vehicle_variants")
    external_id = models.CharField(max_length=220, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["model", "source", "external_id"], condition=~models.Q(external_id=""), name="uniq_vehicle_variant_source_id"),
        ]
        indexes = [models.Index(fields=["model", "year_from", "year_to"], name="core_vehicl_model_i_64e7e2_idx")]

    def __str__(self):
        return f"{self.model} {self.label}".strip()


class CatalogPart(models.Model):
    """Logical catalogue part, distinct from a stock Product or supplier listing."""
    name = models.CharField(max_length=250, blank=True)
    category_name = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]


class CatalogPartNumber(models.Model):
    part = models.ForeignKey(CatalogPart, on_delete=models.CASCADE, related_name="numbers")
    brand = models.CharField(max_length=60)
    brand_norm = models.CharField(max_length=60, db_index=True)
    sku = models.CharField(max_length=80)
    sku_norm = models.CharField(max_length=80, db_index=True)
    source = models.ForeignKey(CatalogSource, on_delete=models.PROTECT, related_name="part_numbers")
    external_id = models.CharField(max_length=180, blank=True)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="catalog_numbers")
    confidence = models.PositiveSmallIntegerField(default=100)
    verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["brand_norm", "sku_norm"], name="uniq_catalog_brand_sku"),
            models.UniqueConstraint(fields=["source", "external_id"], condition=~models.Q(external_id=""), name="uniq_catalog_part_source_id"),
        ]
        indexes = [models.Index(fields=["sku_norm", "brand_norm"], name="core_catalo_sku_nor_3b8f77_idx")]

    def __str__(self):
        return f"{self.brand} {self.sku}"


class PartFitment(models.Model):
    part_number = models.ForeignKey(CatalogPartNumber, on_delete=models.CASCADE, related_name="fitments")
    vehicle_model = models.ForeignKey(VehicleModel, on_delete=models.CASCADE, related_name="part_fitments")
    variant = models.ForeignKey(VehicleVariant, null=True, blank=True, on_delete=models.CASCADE, related_name="part_fitments")
    source = models.ForeignKey(CatalogSource, on_delete=models.PROTECT, related_name="fitments")
    year_from = models.PositiveSmallIntegerField(null=True, blank=True)
    year_to = models.PositiveSmallIntegerField(null=True, blank=True)
    engine_code = models.CharField(max_length=80, blank=True)
    fitment_key = models.CharField(max_length=180, blank=True, default="", editable=False)
    notes = models.CharField(max_length=250, blank=True)
    verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["part_number", "vehicle_model", "source", "fitment_key"],
                                    name="uniq_part_fitment_evidence"),
        ]
        indexes = [models.Index(fields=["vehicle_model", "year_from", "year_to"], name="core_partfi_vehicle_96e2f0_idx")]

    def save(self, *args, **kwargs):
        # Non-null canonical key makes deduplication reliable on SQLite and PostgreSQL,
        # including fitments whose year range or variant is unknown.
        self.fitment_key = (
            f"v:{self.variant_id or 0}|y:{self.year_from or 0}-{self.year_to or 0}|"
            f"e:{(self.engine_code or '').strip().upper()}"
        )[:180]
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = list(kwargs["update_fields"]) + ["fitment_key"]
        super().save(*args, **kwargs)



class Shipment(models.Model):
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.PROTECT, related_name="+", db_index=True)
    """Відправлення Нової пошти: локальна картка, ТТН і останній відомий статус."""
    STATUS = [
        ("draft", "Чернетка"), ("created", "Накладну створено"),
        ("in_transit", "У дорозі"), ("arrived", "Прибуло у відділення"),
        ("delivered", "Отримано"), ("returned", "Повернення"),
        ("problem", "Потрібна увага"),
    ]
    customer = models.ForeignKey("Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="shipments", verbose_name="Клієнт")
    sale = models.ForeignKey("Sale", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="shipments", verbose_name="Продаж")
    recipient_name = models.CharField("Одержувач", max_length=150)
    recipient_phone = models.CharField("Телефон одержувача", max_length=30)
    recipient_city = models.CharField("Місто одержувача", max_length=150, blank=True)
    recipient_city_ref = models.CharField(max_length=80, blank=True)
    recipient_warehouse = models.CharField("Відділення / адреса", max_length=250, blank=True)
    recipient_warehouse_ref = models.CharField(max_length=80, blank=True)
    ttn = models.CharField("ТТН", max_length=40, blank=True, unique=True, null=True)
    description = models.CharField("Опис відправлення", max_length=200, default="Автозапчастини")
    weight = models.DecimalField("Вага, кг", max_digits=8, decimal_places=3, default=1)
    seats_amount = models.PositiveSmallIntegerField("Кількість місць", default=1)
    declared_cost = models.DecimalField("Оголошена вартість, грн", max_digits=12, decimal_places=2, default=0)
    cod_amount = models.DecimalField("Післяплата, грн", max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="draft", db_index=True)
    status_text = models.CharField("Статус перевізника", max_length=250, blank=True)
    last_tracked_at = models.DateTimeField(null=True, blank=True)
    api_error = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="created_shipments")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Відправлення"
        verbose_name_plural = "Відправлення"

    def __str__(self):
        return self.ttn or f"Відправлення #{self.pk}"


from .service_models import ServiceAppointment, ServiceOrder, ServiceOrderLine, ServicePayment  # noqa: E402,F401


# ====================================================== Local store identity
class Company(models.Model):
    """The single local store for this installation; not a SaaS tenant."""
    BUSINESS_TYPES = [
        ("general_retail", "Роздрібна торгівля"),
        ("grocery", "Продуктовий магазин"),
        ("auto_parts", "Автозапчастини"),
        ("electronics", "Електроніка та техніка"),
        ("online_store", "Інтернет-магазин"),
    ]
    name = models.CharField("Назва магазину", max_length=150)
    business_type = models.CharField("Профіль бізнесу", max_length=24, choices=BUSINESS_TYPES, default="auto_parts")
    enabled_modules = models.JSONField("Увімкнені модулі", default=list, blank=True)
    slug = models.SlugField("Ідентифікатор", max_length=180, unique=True)
    owner = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="owned_companies")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Компанія"
        verbose_name_plural = "Компанії"

    def __str__(self):
        return self.name


class StaffMembership(models.Model):
    """Local staff role assignment, independent from the legacy SaaS membership table."""
    ROLES = [
        ("owner", "Власник"), ("admin", "Адміністратор"),
        ("manager", "Менеджер"), ("cashier", "Касир"),
        ("mechanic", "Механік"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="staff_memberships")
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="staff_memberships")
    role = models.CharField(max_length=20, choices=ROLES, default="cashier")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company_id", "user_id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "user"], name="uniq_company_staff_membership"),
        ]
        indexes = [models.Index(fields=["user", "is_active"], name="sm_user_active_idx")]
        verbose_name = "Роль працівника"
        verbose_name_plural = "Ролі працівників"

    def __str__(self):
        return f"{self.user} · {self.company} ({self.get_role_display()})"

    @property
    def is_company_admin(self):
        return self.is_active and self.role in ("owner", "admin")



class LicenseActivation(models.Model):
    """The single signed license installed on this local copy of AutoZapaz."""
    PLANS = [
        ("start", "START"),
        ("pro", "PRO"),
        ("business", "BUSINESS"),
    ]

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    token = models.TextField("Підписаний ліцензійний ключ")
    license_id = models.CharField("ID ліцензії", max_length=80, unique=True)
    plan = models.CharField("Тариф", max_length=20, choices=PLANS)
    issued_at = models.DateTimeField("Видано")
    expires_at = models.DateTimeField("Діє до", null=True, blank=True)
    max_users = models.PositiveIntegerField("Максимум користувачів", default=1)
    max_workstations = models.PositiveIntegerField("Максимум робочих місць", default=1)
    features = models.JSONField("Доступні модулі", default=list, blank=True)
    activated_at = models.DateTimeField("Активовано", auto_now_add=True)
    updated_at = models.DateTimeField("Оновлено", auto_now=True)

    class Meta:
        verbose_name = "Локальна ліцензія"
        verbose_name_plural = "Локальна ліцензія"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.license_id} · {self.get_plan_display()}"


class InstallationIdentity(models.Model):
    """Stable identity for one local AutoZapaz installation."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    installation_id = models.UUIDField("Код інсталяції", default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField("Створено", auto_now_add=True)

    class Meta:
        verbose_name = "Ідентифікатор інсталяції"
        verbose_name_plural = "Ідентифікатор інсталяції"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class LicenseWorkstationSession(models.Model):
    """A recently active authenticated browser session counted as one workstation."""
    session_key = models.CharField("Ключ сесії", max_length=40, unique=True)
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="license_workstation_sessions")
    created_at = models.DateTimeField("Початок сесії", auto_now_add=True)
    last_seen = models.DateTimeField("Остання активність", db_index=True)

    class Meta:
        ordering = ["last_seen"]
        verbose_name = "Активне робоче місце"
        verbose_name_plural = "Активні робочі місця"

    def __str__(self):
        return f"{self.user} · {self.last_seen:%d.%m.%Y %H:%M}"


class FinanceEntry(models.Model):
    """Ручний облік операційних доходів і витрат магазину.

    Не є фіскальним чеком і не змінює касову шухляду: фізичний рух готівки
    залишається в CashOperation, а цей запис формує управлінський фінансовий журнал.
    """
    INCOME, EXPENSE = "income", "expense"
    TYPES = [(INCOME, "Дохід"), (EXPENSE, "Витрата")]
    PAYMENT_METHODS = [
        ("cash", "Готівка"),
        ("card", "Картка"),
        ("bank", "Банківський рахунок"),
    ]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="finance_entries")
    entry_type = models.CharField("Тип операції", max_length=10, choices=TYPES, db_index=True)
    category = models.CharField("Категорія", max_length=80)
    amount = models.DecimalField("Сума", max_digits=14, decimal_places=2)
    payment_method = models.CharField("Спосіб оплати", max_length=10, choices=PAYMENT_METHODS, default="cash")
    description = models.CharField("Опис", max_length=250, blank=True)
    document_ref = models.CharField("Номер документа", max_length=80, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="finance_entries")
    created_at = models.DateTimeField("Дата створення", auto_now_add=True)
    is_void = models.BooleanField("Скасовано", default=False, db_index=True)
    voided_at = models.DateTimeField("Дата скасування", null=True, blank=True)
    voided_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True, related_name="voided_finance_entries")
    void_reason = models.CharField("Причина скасування", max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "created_at"], name="finance_company_date_idx"),
            models.Index(fields=["company", "entry_type", "created_at"], name="finance_company_type_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="finance_entry_amount_positive"),
        ]

    def __str__(self):
        return f"{self.get_entry_type_display()}: {self.amount} ₴ · {self.category}"


class AccountAdvance(models.Model):
    """Customer prepayment held as a liability, or supplier advance paid ahead of a purchase."""
    CUSTOMER, SUPPLIER = "customer", "supplier"
    PARTY_TYPES = [(CUSTOMER, "Аванс клієнта"), (SUPPLIER, "Аванс постачальнику")]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="account_advances")
    party_type = models.CharField(max_length=10, choices=PARTY_TYPES, db_index=True)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT, related_name="account_advances")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT, related_name="account_advances")
    amount = models.DecimalField("Початкова сума", max_digits=14, decimal_places=2)
    remaining = models.DecimalField("Невикористаний залишок", max_digits=14, decimal_places=2)
    payment_method = models.CharField(max_length=10, choices=[("cash", "Готівка"), ("card", "Картка"), ("bank", "Банк")])
    document_ref = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=250, blank=True)
    cash_operation = models.OneToOneField(CashOperation, null=True, blank=True, on_delete=models.PROTECT, related_name="account_advance")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="account_advances")
    created_at = models.DateTimeField(auto_now_add=True)
    is_void = models.BooleanField(default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="voided_account_advances")
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="account_advance_amount_positive"),
            models.CheckConstraint(condition=Q(remaining__gte=0), name="account_advance_remaining_nonnegative"),
        ]
        indexes = [models.Index(fields=["company", "party_type", "created_at"], name="advance_company_type_date_idx")]

    def save(self, *args, **kwargs):
        if self.party_type == self.CUSTOMER:
            if not self.customer_id or self.supplier_id:
                raise ValueError("Аванс клієнта має бути прив'язаний лише до одного клієнта.")
            party = self.customer
        elif self.party_type == self.SUPPLIER:
            if not self.supplier_id or self.customer_id:
                raise ValueError("Аванс постачальнику має бути прив'язаний лише до одного постачальника.")
            party = self.supplier
        else:
            raise ValueError("Невідомий тип авансу.")
        if party.company_id != self.company_id:
            raise ValueError("Контрагент авансу належить іншому магазину.")
        super().save(*args, **kwargs)


class SettlementPayment(models.Model):
    """Auditable payment against a customer sale or supplier purchase document.

    A cash payment is linked to its original cash-drawer movement and, if voided,
    to a separate compensating movement. Card/bank payments never alter the drawer.
    This is not a fiscal receipt or a bank statement integration.
    """
    PAYMENT_METHODS = [
        ("cash", "Готівка"),
        ("card", "Картка"),
        ("bank", "Банківський рахунок"),
        ("advance", "З авансу"),
    ]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="settlement_payments")
    sale = models.ForeignKey("Sale", null=True, blank=True, on_delete=models.PROTECT,
                             related_name="settlement_payments")
    purchase = models.ForeignKey("Purchase", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="settlement_payments")
    advance = models.ForeignKey(AccountAdvance, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="applications")
    amount = models.DecimalField("Сума", max_digits=14, decimal_places=2)
    payment_method = models.CharField("Спосіб оплати", max_length=10, choices=PAYMENT_METHODS, default="cash")
    document_ref = models.CharField("Номер документа оплати", max_length=80, blank=True)
    note = models.CharField("Примітка", max_length=250, blank=True)
    cash_operation = models.OneToOneField(
        CashOperation, null=True, blank=True, on_delete=models.PROTECT,
        related_name="settlement_payment",
    )
    reversal_operation = models.OneToOneField(
        CashOperation, null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversed_settlement_payment",
    )
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="settlement_payments")
    created_at = models.DateTimeField("Дата оплати", auto_now_add=True)
    is_void = models.BooleanField("Скасовано", default=False, db_index=True)
    voided_at = models.DateTimeField("Дата скасування", null=True, blank=True)
    voided_by = models.ForeignKey(USER, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="voided_settlement_payments")
    void_reason = models.CharField("Причина скасування", max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "created_at"], name="settlement_company_date_idx"),
            models.Index(fields=["company", "is_void"], name="settlement_company_void_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="settlement_payment_amount_positive"),
            models.CheckConstraint(
                condition=(Q(sale__isnull=False, purchase__isnull=True) |
                           Q(sale__isnull=True, purchase__isnull=False)),
                name="settlement_exactly_one_document",
            ),
        ]

    def save(self, *args, **kwargs):
        if bool(self.sale_id) == bool(self.purchase_id):
            raise ValueError("Оплата має бути прив'язана рівно до одного документа.")
        document = self.sale if self.sale_id else self.purchase
        if document.company_id != self.company_id:
            raise ValueError("Документ оплати належить іншому магазину.")
        if self.sale_id and not self.sale.customer_id:
            raise ValueError("Для обліку боргу продаж має бути прив'язаний до клієнта.")
        if self.payment_method == "advance":
            if not self.advance_id or self.advance.company_id != self.company_id:
                raise ValueError("Для оплати з авансу потрібен аванс цього магазину.")
            if self.sale_id and (self.advance.party_type != AccountAdvance.CUSTOMER or self.advance.customer_id != self.sale.customer_id):
                raise ValueError("Аванс не належить клієнту цього продажу.")
            if self.purchase_id and (self.advance.party_type != AccountAdvance.SUPPLIER or self.advance.supplier_id != self.purchase.supplier_id):
                raise ValueError("Аванс не належить постачальнику цієї закупівлі.")
        elif self.advance_id:
            raise ValueError("Аванс можна прив'язати лише до оплати з авансу.")
        super().save(*args, **kwargs)

    def __str__(self):
        document = self.sale or self.purchase
        return f"{self.amount} ₴ · {document or 'документ'}"



class BankTransaction(models.Model):
    """Manual bank statement register; not an API connection to a bank."""
    INCOME, EXPENSE = "income", "expense"
    DIRECTIONS = [(INCOME, "Надходження"), (EXPENSE, "Списання")]

    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="bank_transactions")
    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transactions")
    transaction_date = models.DateField("Дата операції", db_index=True)
    direction = models.CharField("Напрямок", max_length=10, choices=DIRECTIONS, db_index=True)
    category = models.CharField("Категорія", max_length=80)
    amount = models.DecimalField("Сума", max_digits=14, decimal_places=2)
    counterparty = models.CharField("Контрагент", max_length=150, blank=True)
    document_ref = models.CharField("Документ / призначення", max_length=120, blank=True)
    note = models.CharField("Примітка", max_length=250, blank=True)
    import_fingerprint = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="bank_transactions")
    created_at = models.DateTimeField(auto_now_add=True)
    is_void = models.BooleanField("Скасовано", default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="voided_bank_transactions")
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-transaction_date", "-id"]
        indexes = [
            models.Index(fields=["company", "account", "transaction_date"], name="bank_tx_account_date_idx"),
            models.Index(fields=["company", "direction", "transaction_date"], name="bank_tx_direction_date_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="bank_transaction_amount_positive"),
            models.UniqueConstraint(fields=["company", "import_fingerprint"], name="bank_import_fingerprint_unique"),
        ]

    def save(self, *args, **kwargs):
        if self.account_id and self.company_id != self.account.company_id:
            raise ValueError("Банківський рахунок належить іншому магазину.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_direction_display()}: {self.amount} ₴ · {self.account.iban}"

class BankReconciliation(models.Model):
    """Auditable match between a bank statement transaction and a recorded settlement payment."""
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="bank_reconciliations")
    bank_transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT, related_name="reconciliations")
    settlement_payment = models.ForeignKey("SettlementPayment", null=True, blank=True, on_delete=models.PROTECT, related_name="bank_reconciliations")
    account_advance = models.ForeignKey("AccountAdvance", null=True, blank=True, on_delete=models.PROTECT, related_name="bank_reconciliations")
    matched_at = models.DateTimeField(auto_now_add=True)
    matched_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="bank_reconciliations_created")
    is_void = models.BooleanField(default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_reconciliations_voided")
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-matched_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["bank_transaction"], condition=Q(is_void=False), name="uniq_active_bank_reconciliation"),
            models.UniqueConstraint(fields=["settlement_payment"], condition=Q(is_void=False), name="uniq_active_payment_reconciliation"),
            models.UniqueConstraint(fields=["account_advance"], condition=Q(is_void=False, account_advance__isnull=False), name="uniq_active_advance_reconciliation"),
            models.CheckConstraint(
                condition=(Q(settlement_payment__isnull=False, account_advance__isnull=True) |
                           Q(settlement_payment__isnull=True, account_advance__isnull=False)),
                name="bank_reconciliation_exactly_one_source",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.bank_transaction.company_id != self.company_id:
            raise ValueError("Банківська операція належить іншому магазину.")
        if bool(self.settlement_payment_id) == bool(self.account_advance_id):
            raise ValueError("Звірка має посилатися рівно на один платіж або аванс.")
        if self.bank_transaction.is_void:
            raise ValueError("Скасовану банківську операцію не можна звіряти.")
        if self.settlement_payment_id:
            payment = self.settlement_payment
            if payment.company_id != self.company_id:
                raise ValueError("Платіж належить іншому магазину.")
            if payment.is_void:
                raise ValueError("Скасовані операції не можна звіряти.")
            if self.bank_transaction.amount != payment.amount:
                raise ValueError("Суми банківської операції та платежу мають збігатися.")
            expected_direction = "income" if payment.sale_id else "expense"
            if self.bank_transaction.direction != expected_direction:
                raise ValueError("Напрямок банківської операції не відповідає документу.")
            if payment.payment_method != "bank":
                raise ValueError("Звіряти можна лише платежі способом «Банк».")
        else:
            advance = self.account_advance
            if advance.company_id != self.company_id:
                raise ValueError("Аванс належить іншому магазину.")
            if advance.is_void:
                raise ValueError("Скасований аванс не можна звіряти.")
            if advance.payment_method != "bank":
                raise ValueError("Звіряти можна лише банківські аванси.")
            if self.bank_transaction.amount != advance.amount:
                raise ValueError("Сума банківської операції має збігатися з початковою сумою авансу.")
            expected_direction = BankTransaction.INCOME if advance.party_type == AccountAdvance.CUSTOMER else BankTransaction.EXPENSE
            if self.bank_transaction.direction != expected_direction:
                raise ValueError("Напрямок банківської операції не відповідає типу авансу.")
        super().save(*args, **kwargs)



class SupplierCreditApplication(models.Model):
    """Transfer excess supplier credit from one purchase document to another."""
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="supplier_credit_applications")
    source_purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="credit_applications_out")
    target_purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="credit_applications_in")
    amount = models.DecimalField("Сума заліку", max_digits=14, decimal_places=2)
    note = models.CharField("Примітка", max_length=250, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="supplier_credit_applications_created")
    created_at = models.DateTimeField(auto_now_add=True)
    is_void = models.BooleanField("Скасовано", default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="supplier_credit_applications_voided")
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["company", "source_purchase"], name="sup_credit_src_idx"),
            models.Index(fields=["company", "target_purchase"], name="sup_credit_dst_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="supplier_credit_application_positive"),
        ]

    def save(self, *args, **kwargs):
        if (self.source_purchase.company_id != self.company_id
                or self.target_purchase.company_id != self.company_id
                or self.source_purchase.supplier_id != self.target_purchase.supplier_id
                or self.source_purchase.supplier.company_id != self.company_id
                or self.source_purchase_id == self.target_purchase_id):
            raise ValueError("Залік кредиту має бути між різними закупівлями одного постачальника й магазину.")
        super().save(*args, **kwargs)


class SupplierReplacementReceipt(models.Model):
    """Physical replacement stock received against a supplier return, without a new payable."""
    company = models.ForeignKey("Company", on_delete=models.PROTECT, related_name="supplier_replacement_receipts")
    supplier_return = models.ForeignKey(SupplierReturn, on_delete=models.PROTECT, related_name="replacement_receipts")
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="supplier_replacement_receipts")
    purchase_item = models.OneToOneField(PurchaseItem, on_delete=models.PROTECT, related_name="supplier_replacement_receipt")
    quantity = models.DecimalField("Кількість заміни", max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField("Облікова собівартість", max_digits=12, decimal_places=2)
    note = models.CharField(max_length=250, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="supplier_replacement_receipts_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="supplier_replacement_qty_positive"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="supplier_replacement_cost_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        if (self.company_id != self.supplier_return.company_id
                or self.company_id != self.purchase.company_id
                or self.company_id != self.purchase_item.company_id
                or self.purchase.supplier_id != self.supplier_return.supplier_id
                or self.purchase_item.purchase_id != self.purchase_id
                or self.purchase_item.product_id != self.supplier_return.purchase_item.product_id):
            raise ValueError("Заміна має відповідати магазину, постачальнику й товару первісного повернення.")
        super().save(*args, **kwargs)
