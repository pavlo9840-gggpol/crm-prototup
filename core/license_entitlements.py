"""Central license plan matrix and route-to-feature mapping for AutoZapaz."""
PLAN_LIMITS = {
    "start": {"max_users": 2, "max_workstations": 1},
    "pro": {"max_users": 5, "max_workstations": 3},
    "business": {"max_users": 20, "max_workstations": 10},
}
PRO_FEATURES = frozenset({
    "supplier_api", "supplier_prices", "vehicle_catalogue", "crossrefs",
    "advanced_pricing", "payroll", "nova_poshta", "advanced_analytics",
    "multi_workstation",
})
PLAN_FEATURES = {
    "start": frozenset(),
    "pro": PRO_FEATURES,
    "business": PRO_FEATURES | frozenset({"service_workshop"}),
}
FEATURE_LABELS = {
    "supplier_api": "API постачальників",
    "supplier_prices": "Прайси постачальників",
    "vehicle_catalogue": "Каталог та підбір за автомобілем",
    "crossrefs": "Аналоги та крос-номери",
    "advanced_pricing": "Розширені правила цін",
    "payroll": "Зарплата менеджерів",
    "nova_poshta": "Інтеграція Нової пошти",
    "advanced_analytics": "Розширена аналітика",
    "multi_workstation": "Кілька робочих місць",
    "service_workshop": "Модуль СТО",
    "advanced_audit": "Розширений аудит",
    "multi_warehouse": "Кілька складів",
    "advanced_integrations": "Розширені інтеграції",
}
NAV_FEATURES = {
    "shipment_list": "nova_poshta",
    "supplier_prices": "supplier_prices",
    "crossrefs": "crossrefs",
    "car_parts": "vehicle_catalogue",
    "catalogue_home": "vehicle_catalogue",
    "parts_import": "vehicle_catalogue",
    "price_rules": "advanced_pricing",
    "price_review": "advanced_pricing",
    "payroll": "payroll",
    "pay_plans": "payroll",
    "manager_sales": "advanced_analytics",
    "service_home": "service_workshop",
}
PATH_FEATURES = (
    ("/supplier-prices/", "supplier_prices"),
    ("/crossrefs/", "crossrefs"),
    ("/car-parts/", "vehicle_catalogue"),
    ("/catalogue/", "vehicle_catalogue"),
    ("/parts-data/", "vehicle_catalogue"),
    ("/part/", "vehicle_catalogue"),
    ("/price-rules/", "advanced_pricing"),
    ("/price-review/", "advanced_pricing"),
    ("/payroll/", "payroll"),
    ("/pay-plans/", "payroll"),
    ("/my-sales/", "advanced_analytics"),
    ("/shipments/", "nova_poshta"),
    ("/service/", "service_workshop"),
)
def enabled_features(claims):
    if not claims:
        return frozenset()
    return PLAN_FEATURES.get(claims.get("plan"), frozenset()) | frozenset(claims.get("features", ()))
def feature_is_enabled(claims, feature):
    return feature in enabled_features(claims)
def feature_for_path(path):
    if path.startswith("/suppliers/") and path.rstrip("/").endswith("/api"):
        return "supplier_api"
    for prefix, feature in PATH_FEATURES:
        if path.startswith(prefix):
            return feature
    return None
