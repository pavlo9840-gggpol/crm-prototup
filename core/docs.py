"""Document data for printed forms: field registry, context builders, sample data, loaders.

Every context is a dict with plain string values:
    {"company": {...}, "account": {...}, "doc": {...}, "items": [ {...}, ... ], "_images": {...}}
"""
from decimal import ROUND_HALF_UP, Decimal

from django.http import Http404
from django.utils import timezone

from .models import (BankAccount, CompanySettings, Product, Purchase, Sale, StockAdjustment,
                     StockMovement)

D0 = Decimal("0")

COMPANY_FIELDS = [
    ("company.name", "Назва магазину"), ("company.legal_name", "Юридична назва"), ("company.tax_id", "ЄДРПОУ / ІПН"),
    ("company.vat_id", "Номер платника ПДВ"), ("company.address", "Адреса"), ("company.phone", "Телефон фірми"),
    ("company.email", "Email фірми"), ("company.website", "Сайт"), ("company.receipt_header", "Текст зверху чека"),
    ("company.receipt_footer", "Текст внизу чека"), ("company.has_logo", "Є логотип (1/порожньо)"),
]
ACCOUNT_FIELDS = [
    ("account.bank_name", "Банк (основний рахунок)"), ("account.iban", "IBAN (основний рахунок)"),
    ("account.mfo", "МФО"), ("account.holder", "Отримувач платежу"),
]
SALE_FIELDS = [
    ("doc.number", "Номер"), ("doc.date", "Дата і час"), ("doc.cashier", "Касир"), ("doc.customer", "Клієнт"),
    ("doc.customer_label", "Клієнт (або «Роздрібний покупець»)"), ("doc.customer_phone", "Телефон клієнта"),
    ("doc.payment", "Спосіб оплати"), ("doc.total", "Сума"), ("doc.vat", "в т.ч. ПДВ"), ("doc.vat_rate", "Ставка ПДВ, %"),
    ("doc.total_novat", "Сума без ПДВ"), ("doc.cash_received", "Отримано готівкою"), ("doc.change", "Здача"),
    ("doc.cash_amount", "Сплачено готівкою"), ("doc.card_amount", "Сплачено карткою"),
    ("doc.qty_total", "Загальна кількість"), ("doc.items_count", "Кількість позицій"),
    ("doc.currency", "Валюта"), ("doc.qr_value", "Дані для QR-коду"),
]
ITEM_COLS = [("n", "№"), ("sku", "Код"), ("barcode", "Штрих-код"), ("name", "Назва"), ("unit", "Од."),
             ("qty", "К-сть"), ("price", "Ціна"), ("total", "Сума")]

DOC_TYPES = {
    "receipt": {"label": "Товарний (касовий) чек", "std_preset": "roll80", "fields": SALE_FIELDS, "cols": ITEM_COLS,
                "has_company": True},
    "sale_invoice": {"label": "Видаткова накладна", "std_preset": "a4", "fields": SALE_FIELDS, "cols": ITEM_COLS,
                     "has_company": True},
    "purchase": {"label": "Прихідна накладна", "std_preset": "a4", "has_company": True, "cols": ITEM_COLS,
                 "fields": [("doc.number", "Номер"), ("doc.date", "Дата і час"), ("doc.supplier", "Постачальник"),
                            ("doc.supplier_phone", "Телефон постачальника"), ("doc.user", "Оператор"),
                            ("doc.total", "Сума"), ("doc.vat", "в т.ч. ПДВ"), ("doc.vat_rate", "Ставка ПДВ, %"),
                            ("doc.total_novat", "Сума без ПДВ"), ("doc.qty_total", "Загальна кількість"),
                            ("doc.items_count", "Кількість позицій"), ("doc.currency", "Валюта")]},
    "writeoff": {"label": "Акт списання / коригування", "std_preset": "a4", "has_company": True,
                 "cols": [("n", "№"), ("sku", "Код"), ("name", "Назва"), ("unit", "Од."), ("qty", "К-сть"),
                          ("price", "Собівартість"), ("total", "Сума")],
                 "fields": [("doc.number", "Номер"), ("doc.date", "Дата і час"), ("doc.user", "Оператор"),
                            ("doc.kind", "Тип (списання/надлишок/нестача)"), ("doc.reason", "Причина"),
                            ("doc.note", "Примітка"), ("doc.total", "Сума"), ("doc.qty_total", "Загальна кількість"),
                            ("doc.currency", "Валюта")]},
    "stock_report": {"label": "Звіт про залишки", "std_preset": "a4", "has_company": True,
                     "cols": [("n", "№"), ("sku", "Код"), ("barcode", "Штрих-код"), ("name", "Назва"),
                              ("category", "Категорія"), ("unit", "Од."), ("qty", "Залишок"), ("min", "Мінімум"),
                              ("cost", "Ціна закупівлі"), ("price", "Ціна продажу"), ("value", "Вартість"),
                              ("status", "Статус")],
                     "fields": [("doc.date", "Дата друку"), ("doc.user", "Користувач"), ("doc.filter", "Застосовані фільтри"),
                                ("doc.count", "Кількість позицій"), ("doc.total_value", "Вартість запасів"),
                                ("doc.currency", "Валюта")]},
    "movement_report": {"label": "Звіт про рух товарів", "std_preset": "a4l", "has_company": True,
                        "cols": [("n", "№"), ("date", "Дата"), ("sku", "Код"), ("name", "Товар"), ("type", "Тип"),
                                 ("change", "Зміна"), ("balance", "Залишок після"), ("doc_ref", "Документ"),
                                 ("user", "Оператор")],
                        "fields": [("doc.date", "Дата друку"), ("doc.user", "Користувач"), ("doc.period", "Період / фільтри"),
                                   ("doc.count", "Кількість записів")]},
    "pricetag": {"label": "Цінник", "std_preset": "tag58", "has_company": True, "cols": [],
                 "fields": [("doc.name", "Назва товару"), ("doc.sku", "Код (SKU)"), ("doc.barcode", "Штрих-код (або код)"),
                            ("doc.price", "Ціна"), ("doc.unit", "Одиниця виміру"), ("doc.category", "Категорія"),
                            ("doc.date", "Дата друку"), ("doc.currency", "Валюта")]},
}


def fields_for(doc_type):
    spec = DOC_TYPES[doc_type]
    groups = [("Документ", spec["fields"])]
    if spec.get("has_company"):
        groups += [("Фірма", COMPANY_FIELDS), ("Рахунок", ACCOUNT_FIELDS)]
    return groups


def all_field_keys(doc_type):
    return {k for _, fl in fields_for(doc_type) for k, _ in fl}


def money(x):
    return f"{Decimal(x):.2f}"


def qty(x):
    return f"{Decimal(x).normalize():f}"


def dt(x):
    return timezone.localtime(x).strftime("%d.%m.%Y %H:%M")


def vat_parts(total, rate):
    total, rate = Decimal(total), Decimal(rate)
    if rate <= 0:
        return D0, total
    vat = (total * rate / (100 + rate)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return vat, total - vat


def base_ctx():
    c = CompanySettings.get()
    acc = BankAccount.objects.first()
    logo = None
    if c.logo:
        try:
            with c.logo.open("rb") as f:
                logo = f.read()
        except OSError:
            logo = None
    return {
        "company": {"name": c.name, "legal_name": c.legal_name, "tax_id": c.tax_id, "vat_id": c.vat_id,
                    "address": c.address, "phone": c.phone, "email": c.email, "website": c.website,
                    "receipt_header": c.receipt_header, "receipt_footer": c.receipt_footer,
                    "has_logo": "1" if logo else ""},
        "account": {"bank_name": acc.bank_name, "iban": acc.iban, "mfo": acc.mfo, "holder": acc.holder} if acc
        else {"bank_name": "", "iban": "", "mfo": "", "holder": ""},
        "_images": {"company_logo": logo}, "_vat_rate": c.vat_rate,
    }


def _rows(pairs):
    return [{"n": str(i), **r} for i, r in enumerate(pairs, 1)]


def sale_ctx(sale):
    ctx = base_ctx()
    items = _rows([{"name": i.product.name, "sku": i.product.sku, "barcode": i.product.barcode or "",
                    "unit": i.product.unit, "qty": qty(i.quantity), "price": money(i.price),
                    "total": money(i.line_total)} for i in sale.items.select_related("product")])
    vat, novat = vat_parts(sale.total, ctx["_vat_rate"])
    pay = dict(Sale.PAYMENTS).get(sale.payment_method, "")
    qsum = sum((i.quantity for i in sale.items.all()), D0)
    ctx["doc"] = {
        "number": sale.number, "date": dt(sale.created_at), "cashier": str(sale.cashier),
        "customer": sale.customer.name if sale.customer else "",
        "customer_label": sale.customer.name if sale.customer else "Роздрібний покупець",
        "customer_phone": sale.customer.phone if sale.customer else "", "payment": pay,
        "total": money(sale.total), "vat": money(vat) if vat else "", "vat_rate": qty(ctx["_vat_rate"]) if vat else "",
        "total_novat": money(novat), "cash_received": money(sale.cash_received) if sale.cash_received else "",
        "change": money(sale.change_given) if sale.cash_received else "",
        "cash_amount": money(sale.cash_amount) if sale.cash_amount else "",
        "card_amount": money(sale.card_amount) if sale.card_amount else "",
        "qty_total": qty(qsum), "items_count": str(len(items)), "currency": "₴",
        "qr_value": f"N={sale.number};D={timezone.localtime(sale.created_at):%Y%m%d%H%M};S={money(sale.total)}"}
    ctx["items"] = items
    return ctx


def purchase_ctx(p):
    ctx = base_ctx()
    items = _rows([{"name": i.product.name, "sku": i.product.sku, "barcode": i.product.barcode or "",
                    "unit": i.product.unit, "qty": qty(i.quantity), "price": money(i.cost_price),
                    "total": money(i.line_total)} for i in p.items.select_related("product")])
    vat, novat = vat_parts(p.total, ctx["_vat_rate"])
    ctx["doc"] = {"number": p.number, "date": dt(p.created_at), "supplier": p.supplier.name,
                  "supplier_phone": p.supplier.phone, "user": str(p.user), "total": money(p.total),
                  "vat": money(vat) if vat else "", "vat_rate": qty(ctx["_vat_rate"]) if vat else "",
                  "total_novat": money(novat),
                  "qty_total": qty(sum((i.quantity for i in p.items.all()), D0)),
                  "items_count": str(len(items)), "currency": "₴"}
    ctx["items"] = items
    return ctx


def writeoff_ctx(a):
    ctx = base_ctx()
    cost = a.product.cost_price
    ctx["items"] = _rows([{"name": a.product.name, "sku": a.product.sku, "unit": a.product.unit,
                           "qty": qty(a.quantity), "price": money(cost), "total": money(a.quantity * cost)}])
    ctx["doc"] = {"number": f"ADJ-{a.pk:06d}", "date": dt(a.created_at), "user": str(a.user),
                  "kind": a.get_kind_display(), "reason": a.get_reason_display() if a.reason else "",
                  "note": a.note, "total": money(a.quantity * cost), "qty_total": qty(a.quantity), "currency": "₴"}
    return ctx


def stock_report_ctx(user, params):
    from django.db.models import F, Q
    ctx = base_ctx()
    qs = Product.objects.select_related("category").filter(is_active=True)
    filt = []
    q, cat, st = params.get("q", "").strip(), params.get("category", ""), params.get("status", "")
    if q:
        qs = qs.filter(Q(name_lc__contains=q.lower()) | Q(sku__icontains=q) | Q(barcode__icontains=q))
        filt.append(f"пошук «{q}»")
    if cat.isdigit():
        qs = qs.filter(category_id=cat)
        filt.append("категорія")
    if st == "out":
        qs = qs.filter(current_stock__lte=0); filt.append("відсутні")
    elif st == "low":
        qs = qs.filter(current_stock__gt=0, current_stock__lte=F("min_stock")); filt.append("низький залишок")
    elif st == "ok":
        qs = qs.filter(current_stock__gt=F("min_stock")); filt.append("норма")
    items, total = [], D0
    for p in qs.order_by("name"):
        val = p.current_stock * p.cost_price
        total += val
        status = "Відсутній" if p.current_stock <= 0 else ("Низький" if p.is_low else "Норма")
        items.append({"sku": p.sku, "barcode": p.barcode or "", "name": p.name, "category": str(p.category or ""),
                      "unit": p.unit, "qty": qty(p.current_stock), "min": qty(p.min_stock), "cost": money(p.cost_price),
                      "price": money(p.price), "value": money(val), "status": status})
    ctx["items"] = _rows(items)
    ctx["doc"] = {"date": dt(timezone.now()), "user": str(user), "filter": ", ".join(filt) or "без фільтрів",
                  "count": str(len(items)), "total_value": money(total), "currency": "₴"}
    return ctx


def movement_report_ctx(user, params):
    ctx = base_ctx()
    qs = StockMovement.objects.select_related("product", "user")
    filt = []
    from datetime import date
    for key, lookup, label in (("from", "created_at__date__gte", "з"), ("to", "created_at__date__lte", "по")):
        try:
            d = date.fromisoformat(params.get(key, ""))
            qs = qs.filter(**{lookup: d}); filt.append(f"{label} {d:%d.%m.%Y}")
        except ValueError:
            pass
    if str(params.get("product", "")).isdigit():
        qs = qs.filter(product_id=params["product"]); filt.append("товар")
    if params.get("type"):
        qs = qs.filter(movement_type=params["type"]); filt.append(dict(StockMovement.TYPES).get(params["type"], ""))
    rows = [{"date": dt(m.created_at), "sku": m.product.sku, "name": m.product.name, "type": m.get_movement_type_display(),
             "change": (("+" if m.quantity_change > 0 else "") + qty(m.quantity_change)), "balance": qty(m.balance_after), "doc_ref": m.doc_ref, "user": str(m.user or "")} for m in qs[:2000]]
    ctx["items"] = _rows(rows)
    ctx["doc"] = {"date": dt(timezone.now()), "user": str(user), "period": ", ".join(filt) or "увесь період",
                  "count": str(len(rows))}
    return ctx


def pricetag_ctx(p):
    ctx = base_ctx()
    ctx["items"] = []
    ctx["doc"] = {"name": p.name, "sku": p.sku, "barcode": p.barcode or p.sku, "price": money(p.price), "unit": p.unit,
                  "category": str(p.category or ""), "date": f"{timezone.localdate():%d.%m.%Y}", "currency": "₴"}
    return ctx


def sample_ctx(doc_type):
    """Fake data for the designer preview."""
    ctx = base_ctx()
    ctx["_images"].setdefault("company_logo", None)
    ctx["company"].update({k: v for k, v in {
        "name": ctx["company"]["name"] or "Мій магазин", "address": ctx["company"]["address"] or "м. Київ, вул. Прикладна, 1",
        "phone": ctx["company"]["phone"] or "+380441234567", "tax_id": ctx["company"]["tax_id"] or "12345678",
        "legal_name": ctx["company"]["legal_name"] or "ФОП Іваненко І.І."}.items()})
    if not ctx["account"]["iban"]:
        ctx["account"] = {"bank_name": "ПриватБанк", "iban": "UA213223130000026007233566001", "mfo": "305299",
                          "holder": "ФОП Іваненко І.І."}
    names = [("Кола 0.5л", "COLA05", "4820000000011", "шт", "2", "30.00"), ("Хліб білий нарізний", "BREAD1", "4820000000028", "шт", "1", "25.50"),
             ("Молоко 2.5% 0.9л", "MILK09", "4820000000035", "шт", "3", "42.00"), ("Яблука Голден (вагові)", "APPLE", "", "кг", "1.35", "39.90"),
             ("Печиво «Марія» з дуже довгою назвою для перевірки переносу", "BISC1", "4820000000042", "пачка", "4", "18.75")]
    items = _rows([{"name": n, "sku": s, "barcode": b, "unit": u, "qty": q, "price": p,
                    "total": money(Decimal(q) * Decimal(p))} for n, s, b, u, q, p in names])
    total = sum((Decimal(i["total"]) for i in items), D0)
    vat, novat = vat_parts(total, Decimal("20"))
    ctx["items"] = items
    ctx["doc"] = {"number": "CH-000123", "date": "05.10.2026 14:32", "cashier": "cashier", "user": "admin",
                  "customer": "Іван Петренко", "customer_label": "Іван Петренко", "customer_phone": "+380501112233",
                  "supplier": "ТОВ «Постачальник»", "supplier_phone": "+380441234567", "payment": "Готівка",
                  "total": money(total), "vat": money(vat), "vat_rate": "20", "total_novat": money(novat),
                  "cash_received": "500.00", "change": money(Decimal("500") - total), "cash_amount": money(total),
                  "card_amount": "", "qty_total": "11.35", "items_count": str(len(items)), "currency": "₴",
                  "qr_value": "N=CH-000123;D=202610051432;S=" + money(total),
                  "kind": "Списання", "reason": "Пошкодження", "note": "Розбита пляшка", "filter": "без фільтрів",
                  "count": str(len(items)), "total_value": money(total), "period": "з 01.10.2026 по 05.10.2026",
                  "name": "Кола 0.5л", "sku": "COLA05", "barcode": "4820000000011", "price": "30.00", "unit": "шт",
                  "category": "Напої → Газовані напої"}
    if doc_type == "writeoff":
        ctx["items"] = ctx["items"][:1]
    if doc_type == "stock_report":
        ctx["items"] = _rows([{"sku": i["sku"], "barcode": i["barcode"], "name": i["name"], "category": "Напої", "unit": i["unit"],
                               "qty": "120", "min": "20", "cost": "20.00", "price": i["price"], "value": "2400.00",
                               "status": "Норма"} for i in items])
    if doc_type == "movement_report":
        ctx["items"] = _rows([{"date": "05.10.2026 14:32", "sku": i["sku"], "name": i["name"], "type": "Видаток продажу",
                               "change": "-2", "balance": "98", "doc_ref": "CH-000123", "user": "cashier"} for i in items])
    if doc_type == "pricetag":
        ctx["items"] = []
    return ctx


# ---------- loaders (real data) ----------
def parse_ids(request):
    ids = []
    for raw in request.GET.getlist("ids"):
        for part in raw.split(","):
            if part.strip().isdigit():
                ids.append(int(part))
    return ids[:500]


def _by_ids(model_qs, ids):
    objs = {o.pk: o for o in model_qs.filter(pk__in=ids)}
    return [objs[i] for i in ids if i in objs]


def load(doc_type, request):
    """Return a list of contexts for the requested documents."""
    user, ids = request.user, parse_ids(request)
    if doc_type in ("receipt", "sale_invoice"):
        qs = Sale.objects.filter(status=Sale.DONE).select_related("cashier", "customer")
        if not user.is_staff:
            qs = qs.filter(cashier=user)
        out = [sale_ctx(s) for s in _by_ids(qs, ids)]
    elif doc_type == "purchase":
        out = [purchase_ctx(p) for p in _by_ids(Purchase.objects.select_related("supplier", "user"), ids)]
    elif doc_type == "writeoff":
        out = [writeoff_ctx(a) for a in _by_ids(StockAdjustment.objects.select_related("product", "user"), ids)]
    elif doc_type == "pricetag":
        try:
            copies = max(1, min(100, int(request.GET.get("copies", 1))))
        except ValueError:
            copies = 1
        out = [pricetag_ctx(p) for p in _by_ids(Product.objects.select_related("category"), ids) for _ in range(copies)]
    elif doc_type == "stock_report":
        out = [stock_report_ctx(user, request.GET)]
    elif doc_type == "movement_report":
        out = [movement_report_ctx(user, request.GET)]
    else:
        raise Http404
    if not out:
        raise Http404("Документи не знайдено")
    return out


def receipt_text(ctx, cols=42):
    """Plain-text receipt for thermal printers (does not use the visual template)."""
    import textwrap
    c, d = ctx["company"], ctx["doc"]
    out = []
    for v in (c["name"], c["address"], ("Тел.: " + c["phone"]) if c["phone"] else "", ("ЄДРПОУ/ІПН: " + c["tax_id"]) if c["tax_id"] else ""):
        if v:
            out += [l.center(cols) for l in textwrap.wrap(v, cols)]
    out += ["-" * cols, f"Чек № {d['number']}", d["date"], f"Касир: {d['cashier']}"]
    if d["customer"]:
        out.append(f"Клієнт: {d['customer']}")
    out.append("-" * cols)
    for it in ctx["items"]:
        out += textwrap.wrap(f"{it['n']}. {it['name']}", cols) or [""]
        left, right = f"   {it['qty']} {it['unit']} x {it['price']}", it["total"]
        out.append(left + right.rjust(max(1, cols - len(left))))
    out.append("-" * cols)
    tot = f"РАЗОМ: {d['total']} грн"
    out.append(tot.rjust(cols))
    if d["vat"]:
        out.append(f"в т.ч. ПДВ: {d['vat']}".rjust(cols))
    out.append(f"Оплата: {d['payment']}")
    if d["cash_received"]:
        out += [f"Отримано готівкою: {d['cash_received']}", f"Здача: {d['change']}"]
    if d["card_amount"]:
        out.append(f"Карткою: {d['card_amount']}")
    if c["receipt_footer"]:
        out += ["-" * cols] + [l.center(cols) for p in c["receipt_footer"].splitlines() for l in (textwrap.wrap(p, cols) or [""])]
    return "\n".join(out)
