from .models import CompanySettings

# (group title, [(label, url name)])
STAFF_NAV = [
    ("Огляд", [("Головна", "dashboard")]),
    ("Торгівля", [("Каса", "pos"), ("Продажі", "sale_list"), ("Відкладені чеки", "held_list"), ("Зміна", "shift"),
                  ("Готівка в касі", "drawer"), ("Замовлення в дорозі", "orders"), ("Приймання накладної", "receive_home"), ("Підбір за авто", "car_parts"), ("Каталог запчастин", "catalogue_home"),
                  ("Продажі менеджерів і маржа", "manager_sales"), ("Зарплата менеджерів", "payroll"), ("Плани оплати", "pay_plans")]),
    ("Склад", [("Товари", "product_list"), ("Категорії", "category_list"), ("Запаси", "inventory"),
               ("Списання / коригування", "adjustment_list"), ("Закупівлі", "purchase_list"),
               ("Імпорт / експорт товарів", "product_import")]),
    ("Постачальники та каталог", [("Постачальники", "supplier_list"), ("Прайси постачальників", "supplier_prices"), ("Курси валют", "rates"),
                                  ("Аналоги (кроси)", "crossrefs"), ("OE-номери та описи", "parts_import"),
                                  ("Націнки клієнтів", "markup_list"), ("Правила цін", "price_rules"), ("Перегляд цін", "price_review")]),
    ("Клієнти", [("Клієнти", "customer_list")]),
    ("Звіти", [("Денний", "report_daily"), ("Місячний", "report_monthly"), ("Рейтинг товарів", "report_top"),
               ("Маржа", "report_profit"), ("Запаси (екран)", "report_stock"),
               ("Друк: залишки", "print_stock_report"), ("Друк: рух товарів", "print_movement_report")]),
    ("Бланки та друк", [("Дизайнер бланків", "designer_home")]),
    ("Налаштування", [("Фірма та рахунки", "company_settings"), ("Користувачі", "user_list"), ("Резервні копії", "backup")]),
]
CASHIER_NAV = [("Каса", "pos"), ("Відкладені", "held_list"), ("Замовлення", "orders"), ("Приймання", "receive_home"), ("Підбір за авто", "car_parts"), ("Мої продажі", "manager_sales"), ("Чеки", "sale_list"),
               ("Готівка", "drawer"), ("Зміна", "shift")]

# inner pages -> the menu item they belong to (keeps the right item highlighted and its group open)
ALIASES = {
    "product_new": "product_list", "product_edit": "product_list", "category_new": "category_list",
    "category_edit": "category_list", "supplier_new": "supplier_list", "supplier_edit": "supplier_list",
    "purchase_new": "purchase_list", "purchase_detail": "purchase_list", "sale_detail": "sale_list",
    "inventory_adjust": "adjustment_list", "inventory_movements": "inventory", "customer_new": "customer_list",
    "customer_edit": "customer_list", "part_card": "product_list", "markup_new": "markup_list", "markup_edit": "markup_list",
    "price_map": "supplier_prices", "pay_plan_new": "pay_plans", "pay_plan_edit": "pay_plans", "price_rule_new": "price_rules", "price_rule_edit": "price_rules", "receive_preview": "receive_home", "supplier_api": "supplier_prices", "customer_detail": "customer_list", "designer_select": "designer_home",
    "designer_edit": "designer_home", "user_new": "user_list", "user_edit": "user_list", "print_docs": "designer_home",
}


ICONS = {"Огляд": "🏠", "Торгівля": "🛒", "Склад": "📦", "Постачальники та каталог": "🔧", "Клієнти": "👥",
         "Звіти": "📊", "Бланки та друк": "🖨️", "Налаштування": "⚙️"}


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
            groups.append({"title": title, "icon": ICONS.get(title, "•"), "items": rows, "has_active": any(r["active"] for r in rows),
                           "single": len(rows) == 1})
    chip = []
    try:                                              # курси для індикатора у верхній панелі (EUR/USD)
        from . import currency
        late = currency.latest_rates()
        chip = [{"code": k, "rate": late[k].rate, "stale": currency.is_stale(late[k])} for k in ("EUR", "USD") if k in late]
    except Exception:                                 # індикатор не має ламати сторінки (напр. до міграції)
        chip = []
    return {"nav_groups": groups, "cashier_nav": CASHIER_NAV, "cur_url": cur, "rates_chip": chip,
            "company_name": CompanySettings.get().name}
