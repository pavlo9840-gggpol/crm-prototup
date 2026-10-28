from .models import CompanySettings

# Основна навігація: щоденні операції окремо, рідше потрібні інструменти — у групах.
STAFF_NAV = [
    ("Огляд", [("Головна", "dashboard")]),
    ("Продажі", [
        ("Каса", "pos"), ("Продажі та повернення", "sale_list"),
        ("Відкладені чеки", "held_list"), ("Зміна касира", "shift"),
        ("Готівка в касі", "drawer"), ("Клієнти", "customer_list"),
    ]),
    ("Замовлення", [
        ("Замовлення в дорозі", "orders"), ("Приймання накладної", "receive_home"),
        ("Відправлення Нової пошти", "shipment_list"),
        ("Закупівлі", "purchase_list"), ("Продажі менеджерів і маржа", "manager_sales"),
    ]),
    ("Склад", [
        ("Залишки", "inventory"), ("Товари", "product_list"),
        ("Рух товарів", "inventory_movements"), ("Списання / коригування", "adjustment_list"),
        ("Категорії", "category_list"), ("Імпорт / експорт товарів", "product_import"),
    ]),
    ("Постачальники та ціни", [
        ("Постачальники", "supplier_list"), ("Прайси постачальників", "supplier_prices"),
        ("Курси валют", "rates"), ("Націнки клієнтів", "markup_list"),
        ("Правила цін", "price_rules"), ("Перегляд цін", "price_review"),
    ]),
    ("Каталог автозапчастин", [
        ("Підбір за авто", "car_parts"), ("Каталог запчастин", "catalogue_home"),
        ("Аналоги (кроси)", "crossrefs"), ("OE-номери та описи", "parts_import"),
    ]),
    ("Звіти", [
        ("Денний звіт", "report_daily"), ("Місячний звіт", "report_monthly"),
        ("Рейтинг товарів", "report_top"), ("Прибуток і маржа", "report_profit"),
        ("Залишки (звіт)", "report_stock"), ("Друк: залишки", "print_stock_report"),
        ("Друк: рух товарів", "print_movement_report"),
    ]),
    ("Керування", [
        ("Зарплата менеджерів", "payroll"), ("Плани оплати", "pay_plans"),
        ("Дизайнер бланків", "designer_home"), ("Фірма та рахунки", "company_settings"),
        ("Користувачі", "user_list"), ("Резервні копії", "backup"),
    ]),
]
CASHIER_NAV = [
    ("Каса", "pos"), ("Відкладені", "held_list"), ("Замовлення", "orders"),
    ("Приймання", "receive_home"), ("Підбір за авто", "car_parts"),
    ("Мої продажі", "manager_sales"), ("Чеки", "sale_list"),
    ("Готівка", "drawer"), ("Зміна", "shift"),
]

# Внутрішні сторінки підсвічують батьківський пункт меню.
ALIASES = {
    "product_new": "product_list", "product_edit": "product_list",
    "category_new": "category_list", "category_edit": "category_list",
    "supplier_new": "supplier_list", "supplier_edit": "supplier_list",
    "purchase_new": "purchase_list", "purchase_detail": "purchase_list",
    "sale_detail": "sale_list", "sale_return": "sale_list",
    "inventory_adjust": "adjustment_list", "inventory_movements": "inventory",
    "customer_new": "customer_list", "customer_edit": "customer_list",
    "part_card": "product_list", "markup_new": "markup_list", "markup_edit": "markup_list",
    "price_map": "supplier_prices", "pay_plan_new": "pay_plans", "pay_plan_edit": "pay_plans",
    "price_rule_new": "price_rules", "price_rule_edit": "price_rules",
    "receive_preview": "receive_home", "receive_apply": "receive_home",
    "supplier_api": "supplier_list", "customer_detail": "customer_list",
    "designer_select": "designer_home", "designer_edit": "designer_home",
    "designer_save": "designer_home", "designer_save_as": "designer_home",
    "designer_new": "designer_home", "designer_copy": "designer_home",
    "designer_delete": "designer_home", "designer_default": "designer_home",
    "designer_export": "designer_home", "designer_import": "designer_home",
    "designer_preview": "designer_home", "user_new": "user_list", "user_edit": "user_list",
    "print_docs": "designer_home",
}

ICONS = {
    "Огляд": "🏠", "Продажі": "🧾", "Замовлення": "🚚", "Склад": "📦",
    "Постачальники та ціни": "🏷️", "Каталог автозапчастин": "🔎",
    "Звіти": "📊", "Керування": "⚙️",
}


def layout(request):
    u = getattr(request, "user", None)
    if not u or not u.is_authenticated:
        return {}
    m = request.resolver_match
    cur = m.url_name if m else ""
    cur = ALIASES.get(cur, cur)
    if cur == "print_docs" or (m and m.url_name == "print_docs"):
        cur = "designer_home"
    groups = []
    if u.is_staff:
        for title, items in STAFF_NAV:
            rows = [{"label": lbl, "name": name, "active": name == cur} for lbl, name in items]
            groups.append({
                "title": title, "icon": ICONS.get(title, "•"), "items": rows,
                "has_active": any(r["active"] for r in rows), "single": len(rows) == 1,
            })
    chip = []
    try:
        from . import currency
        late = currency.latest_rates()
        chip = [{"code": k, "rate": late[k].rate, "stale": currency.is_stale(late[k])}
                for k in ("EUR", "USD") if k in late]
    except Exception:
        chip = []
    return {
        "nav_groups": groups, "cashier_nav": CASHIER_NAV, "cur_url": cur,
        "rates_chip": chip, "company_name": CompanySettings.get().name,
    }
