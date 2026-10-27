"""Правила цін: ціна продажу = закупівельна + націнка, але не нижча за мінімальну маржу, з округленням вгору.

Яка націнка береться (від найважливішої):
  1. явна категорія цін клієнта (напр. СТО +15%): це його умови;
  2. націнка правила для бренду/категорії;
  3. типова націнка (довідник «Націнки клієнтів»).
Мінімальна маржа й округлення з правила діють завжди. Найконкретніше правило перемагає: бренд + категорія, потім бренд, потім категорія, потім загальне.
"""
import math
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Min

from .models import Category, PriceHistory, PriceRule, Product, SupplierItem

D0, CENT = Decimal("0"), Decimal("0.01")


class RuleSet:
    """Усі активні правила в памʼяті (для масових розрахунків без запитів на кожен товар)."""

    def __init__(self):
        self.rules = list(PriceRule.objects.filter(is_active=True))
        self.parent = dict(Category.objects.values_list("pk", "parent_id")) if any(r.category_id for r in self.rules) else {}

    def chain(self, category_id):
        out, seen = [], set()
        while category_id and category_id not in seen:
            out.append(category_id)
            seen.add(category_id)
            category_id = self.parent.get(category_id)
        return out

    def for_(self, brand, category_id=None):
        brand, chain = (brand or "").upper(), self.chain(category_id)
        best, best_key = None, None
        for r in self.rules:
            if r.brand and r.brand.upper() != brand:
                continue
            if r.category_id and r.category_id not in chain:
                continue
            depth = (len(chain) - chain.index(r.category_id)) if r.category_id else 0     # ближча категорія конкретніша
            key = (1 if r.brand else 0, 1 if r.category_id else 0, depth, -r.priority, -r.pk)
            if best_key is None or key > best_key:
                best, best_key = r, key
        return best


def compute_price(cost, pct, rule=None):
    cost = Decimal(cost)
    price = cost * (Decimal(100) + Decimal(pct)) / Decimal(100)
    if rule is not None:
        if rule.min_margin_pct and rule.min_margin_pct > 0 and rule.min_margin_pct < 100:
            price = max(price, cost / (Decimal(1) - rule.min_margin_pct / Decimal(100)))
        if rule.rounding:
            step = Decimal(rule.rounding)
            price = (price / step).to_integral_value(rounding="ROUND_CEILING") * step
    return price.quantize(CENT, ROUND_HALF_UP)


def base_pct(customer, rule, default_pct=None):
    from . import services
    if customer is not None and customer.markup_tier_id:
        return customer.markup_tier.percent
    if rule is not None and rule.markup_pct is not None:
        return rule.markup_pct
    return default_pct if default_pct is not None else services.tier_pct(None)


def list_price(cost, brand, category_id=None, customer=None, rs=None):
    """(ціна, правило) для закупівельної ціни `cost`."""
    rs = rs or RuleSet()
    rule = rs.for_(brand, category_id)
    return compute_price(cost, base_pct(customer, rule), rule), rule


def rule_json(rule):
    """Для каси: те саме обчислення виконується в браузері, тож передаємо параметри правила."""
    if rule is None:
        return None
    return {"mk": float(rule.markup_pct) if rule.markup_pct is not None else None, "mm": float(rule.min_margin_pct or 0),
            "rd": int(rule.rounding or 0)}


def margin_pct(price, cost):
    price, cost = Decimal(price), Decimal(cost)
    return ((price - cost) / price * 100) if price > 0 else D0


def replacement_costs(products):
    """{(БРЕНД, артикул_норм): мінімальна закупівельна ціна серед доступних позицій постачальників}."""
    norms = {p.sku_norm for p in products if p.sku_norm}
    if not norms:
        return {}
    qs = (SupplierItem.objects.filter(available=True, sku_norm__in=norms, cost__gt=0).values("brand", "sku_norm").annotate(c=Min("cost")))
    return {(r["brand"].upper(), r["sku_norm"]): r["c"] for r in qs}


def reprice_product(product, user=None, cost=None, rs=None):
    """Перераховує ціну продажу за правилами від `cost` (або від закупівельної ціни товару).
    Повертає (стара, нова) або None, якщо нічого не змінилось. Зміна пишеться в історію цін."""
    p = Product.objects.get(pk=product.pk)
    base = Decimal(cost) if cost is not None else p.cost_price
    if not base or base <= 0:
        return None
    rs = rs or RuleSet()
    rule = rs.for_(p.brand, p.category_id)
    new = compute_price(base, base_pct(None, rule), rule)
    if new == p.price:
        return None
    PriceHistory.objects.create(product=p, field="price", old_value=p.price, new_value=new, changed_by=user)
    Product.objects.filter(pk=p.pk).update(price=new)
    return p.price, new


def reprice_after_supplier_change(changed_keys, user=None):
    """Після оновлення прайсу: автоціна товарів, чиї постачальницькі позиції змінились. changed_keys: {(БРЕНД, артикул_норм)}."""
    if not changed_keys:
        return 0
    prods = [p for p in Product.objects.filter(auto_price=True, sku_norm__in={n for _, n in changed_keys})
             if (p.brand.upper(), p.sku_norm) in changed_keys]
    if not prods:
        return 0
    repl, rs, n = replacement_costs(prods), RuleSet(), 0
    for p in prods:
        c = repl.get((p.brand.upper(), p.sku_norm))
        if c and reprice_product(p, user, cost=c, rs=rs):
            n += 1
    return n
