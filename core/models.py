import re
from decimal import Decimal
from django.conf import settings
from django.db import models

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
    name = models.CharField("Назва", max_length=200)
    sku = models.CharField("Артикул", max_length=50, db_index=True)
    barcode = models.CharField("Штрих-код", max_length=50, unique=True, null=True, blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name="Категорія")
    unit = models.CharField("Одиниця виміру", max_length=20, default="шт")
    cost_price = models.DecimalField("Ціна закупівлі", max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField("Ціна продажу", max_digits=10, decimal_places=2, default=0)
    allow_price_change = models.BooleanField("Дозволити зміну ціни на касі", default=False)
    current_stock = models.DecimalField("Залишок", max_digits=12, decimal_places=3, default=0)
    min_stock = models.DecimalField("Мінімальний залишок", max_digits=12, decimal_places=3, default=0)
    is_active = models.BooleanField("Активний", default=True)
    is_favorite = models.BooleanField("Швидка кнопка на касі", default=False)
    photo = models.ImageField("Фото", upload_to="products/", blank=True, null=True)
    notes = models.TextField("Примітки", blank=True)
    brand = models.CharField("Бренд (виробник)", max_length=60, blank=True, db_index=True)
    sku_norm = models.CharField(max_length=60, blank=True, db_index=True, editable=False)
    name_lc = models.CharField(max_length=300, blank=True, editable=False)     # назва малими літерами: для пошуку по кирилиці
    from_supplier = models.BooleanField("Створено з прайсу постачальника", default=False, editable=False)
    auto_price = models.BooleanField("Автоціна за правилами", default=False,
                                     help_text="Ціна продажу сама перераховується за правилами цін, коли змінюється закупівельна.")

    class Meta:
        ordering = ["name"]
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


class PriceHistory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="price_history")
    field = models.CharField(max_length=10)  # price / cost_price
    old_value = models.DecimalField(max_digits=10, decimal_places=2)
    new_value = models.DecimalField(max_digits=10, decimal_places=2)
    changed_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)
    changed_at = models.DateTimeField(auto_now_add=True)


class Customer(models.Model):
    name = models.CharField("Ім'я", max_length=100)
    phone = models.CharField("Телефон", max_length=30, unique=True)
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
        verbose_name = "Клієнт"
        verbose_name_plural = "Клієнти"

    def __str__(self):
        return f"{self.name} ({self.phone})"


class Shift(models.Model):
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
    DONE, HELD = "done", "held"
    PAYMENTS = [("cash", "Готівка"), ("card", "Картка"), ("mixed", "Змішана")]
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
    number = models.CharField(max_length=20, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)


class SaleReturnItem(models.Model):
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    price = models.DecimalField(max_digits=10, decimal_places=2)


class Purchase(models.Model):
    number = models.CharField(max_length=20, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=15, default="Підтверджено")

    class Meta:
        ordering = ["-created_at"]


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)

    @property
    def line_total(self):
        return self.quantity * self.cost_price


class StockAdjustment(models.Model):
    KINDS = [("writeoff", "Списання"), ("surplus", "Надлишок"), ("shortage", "Нестача")]
    REASONS = [("damaged", "Пошкодження"), ("expired", "Прострочення"), ("other", "Інше")]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="Товар")
    kind = models.CharField("Тип", max_length=10, choices=KINDS)
    quantity = models.DecimalField("Кількість", max_digits=10, decimal_places=3)
    reason = models.CharField("Причина", max_length=10, choices=REASONS, blank=True)
    note = models.TextField("Примітка", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT)


class StockMovement(models.Model):
    TYPES = [
        ("purchase", "Надходження"), ("sale", "Видаток продажу"),
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

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BankAccount(models.Model):
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
    """LEGACY (v2 HTML templates). Kept so existing data is not lost; replaced by PrintTemplate."""
    kind = models.CharField(max_length=20, unique=True)
    body = models.TextField()
    paper = models.CharField(max_length=10, default="80mm")
    updated_at = models.DateTimeField(auto_now=True)


class PrintTemplate(models.Model):
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
    """Пара аналогів (за нормалізованим артикулом), симетрична."""
    a = models.CharField(max_length=80, db_index=True)
    b = models.CharField(max_length=80, db_index=True)
    a_raw = models.CharField(max_length=80, blank=True)
    b_raw = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=10, default="import")        # manual — підтверджено людиною

    class Meta:
        unique_together = [("a", "b")]


class MarkupTier(models.Model):
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
    """Пара, яку людина визнала НЕ аналогами: приховується з усіх автоматичних джерел."""
    a = models.CharField(max_length=80)
    b = models.CharField(max_length=80)
    user = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("a", "b")]


class CustomerCar(models.Model):
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
    """Журнал змін закупівельної ціни в прайсах постачальників."""
    item = models.ForeignKey(SupplierItem, on_delete=models.CASCADE, related_name="changes")
    old_cost = models.DecimalField(max_digits=12, decimal_places=2)
    new_cost = models.DecimalField(max_digits=12, decimal_places=2)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at", "-pk"]


class PayPlan(models.Model):
    """План оплати менеджера: відсоток від маржі + фіксована частина. Запис без менеджера = типовий план."""
    user = models.OneToOneField(USER, null=True, blank=True, on_delete=models.CASCADE, related_name="pay_plan",
                                verbose_name="Менеджер (порожньо = типовий план для всіх)")
    margin_pct = models.DecimalField("Відсоток від маржі, %", max_digits=5, decimal_places=2, default=10)
    fixed_monthly = models.DecimalField("Фіксована частина на місяць, ₴", max_digits=10, decimal_places=2, default=0)
    note = models.CharField("Примітка", max_length=200, blank=True)

    class Meta:
        verbose_name = "План оплати"


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
    source_id = models.CharField(max_length=180, blank=True)
    name_norm = models.CharField(max_length=140, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "slug"], name="uniq_vehicle_make_kind_slug"),
            models.UniqueConstraint(fields=["source", "source_id"], condition=~models.Q(source_id=""), name="uniq_vehicle_make_source_id"),
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
    source_id = models.CharField(max_length=220, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["model", "source", "source_id"], condition=~models.Q(source_id=""), name="uniq_vehicle_variant_source_id"),
        ]
        indexes = [models.Index(fields=["model", "year_from", "year_to"])]

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
    source_id = models.CharField(max_length=180, blank=True)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="catalog_numbers")
    confidence = models.PositiveSmallIntegerField(default=100)
    verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["brand_norm", "sku_norm"], name="uniq_catalog_brand_sku"),
            models.UniqueConstraint(fields=["source", "source_id"], condition=~models.Q(source_id=""), name="uniq_catalog_part_source_id"),
        ]
        indexes = [models.Index(fields=["sku_norm", "brand_norm"])]

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
    notes = models.CharField(max_length=250, blank=True)
    verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["part_number", "vehicle_model", "variant", "source", "year_from", "year_to", "engine_code"],
                                    name="uniq_part_fitment_evidence"),
        ]
        indexes = [models.Index(fields=["vehicle_model", "year_from", "year_to"])]

