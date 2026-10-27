"""Аналоги запчастин: безкоштовно й автоматично, з оцінкою довіри.

Принцип TecDoc: деталі, привʼязані до одного OE-номера, взаємозамінні. Ліцензованої бази TecDoc немає, тому докази збираються
з кількох НЕЗАЛЕЖНИХ джерел, і чим більше їх збігається, тим вища довіра:
  manual  людина підтвердила пару (100)                  oe     спільний OE-номер (70-90 залежно від кількості спільних)
  api     кросс із API постачальника (55)                 import кросс із файлу (45)
  name    збіг за назвою: тип + авто + позиція (до 40)    chain  ланцюжок кросів (20)
Пара, яку людина відхилила (CrossReject), не показується жодним джерелом.
"""
import re

from django.db.models import Q, Sum

from .models import CrossReject, CrossRef, PartOe, Product, SupplierItem, norm_sku

RANK = {"manual": 4, "oe": 3, "cross": 2, "name": 1, "maybe": 0}
LABEL = {"manual": "Підтверджено вручну", "oe": "Точний аналог (спільний OE)", "cross": "Кросс",
         "name": "Збіг за назвою, перевірте", "maybe": "Ймовірний, перевірте"}
KIND_LEVEL = {"manual": "manual", "oe": "oe", "api": "cross", "import": "cross", "name": "name", "chain": "maybe"}
WEIGHT = {"manual": 100, "api": 55, "import": 45, "chain": 20}


def pair(a, b):
    return tuple(sorted((a, b)))


def rejected(qn):
    out = set()
    for a, b in CrossReject.objects.filter(Q(a=qn) | Q(b=qn)).values_list("a", "b"):
        out.add(b if a == qn else a)
    return out


def analog_levels(qn):
    """{нормалізований артикул: найкращий рівень} для аналогів артикула qn (будь-якого бренду).
    Якщо qn сам є OE-номером, повертаються всі деталі, привʼязані до нього."""
    out, rej = {}, rejected(qn)

    def put(n, lvl):
        if n and n != qn and n not in rej and RANK[lvl] > RANK.get(out.get(n), -1):
            out[n] = lvl

    own_oes = set(PartOe.objects.filter(sku_norm=qn).values_list("oe_norm", flat=True))
    own_oes.add(qn)                                  # менеджер міг ввести саме OE-номер
    for n in PartOe.objects.filter(oe_norm__in=own_oes).values_list("sku_norm", flat=True):
        put(n, "oe")
    first = set()
    for a, b, src in CrossRef.objects.filter(Q(a=qn) | Q(b=qn)).values_list("a", "b", "source"):
        other = b if a == qn else a
        first.add(other)
        put(other, "manual" if src == "manual" else "cross")
    if first:
        for a, b in CrossRef.objects.filter(Q(a__in=first) | Q(b__in=first)).values_list("a", "b"):
            put(a, "maybe")
            put(b, "maybe")
    return out


def analog_norms(qn):
    return set(analog_levels(qn))


# ---------------------------------------------------------------- підтвердження / відхилення людиною
def confirm_pair(user, a_norm, b_norm):
    x, y = pair(a_norm, b_norm)
    obj, created = CrossRef.objects.get_or_create(a=x, b=y, defaults={"a_raw": x, "b_raw": y, "source": "manual"})
    if not created and obj.source != "manual":
        obj.source = "manual"
        obj.save(update_fields=["source"])
    CrossReject.objects.filter(a=x, b=y).delete()


def reject_pair(user, a_norm, b_norm):
    x, y = pair(a_norm, b_norm)
    CrossReject.objects.get_or_create(a=x, b=y, defaults={"user": user})
    CrossRef.objects.filter(a=x, b=y, source="manual").delete()      # підтвердження, яке скасовано


# ---------------------------------------------------------------- виведення за назвою
POSITION = {"передній": "F", "передня": "F", "передні": "F", "переднє": "F", "перед": "F", "front": "F",
            "задній": "R", "задня": "R", "задні": "R", "заднє": "R", "зад": "R", "rear": "R",
            "лівий": "L", "ліва": "L", "ліве": "L", "ліві": "L", "лів": "L", "left": "L",
            "правий": "P", "права": "P", "праве": "P", "праві": "P", "прав": "P", "right": "P",
            "верхній": "U", "верхня": "U", "нижній": "D", "нижня": "D"}
STOP = {"для", "на", "та", "із", "з", "і", "в", "the", "for", "and", "комплект", "компл", "шт", "set", "kit", "новий"}
TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+")


def name_profile(name, brand=""):
    """Тип деталі (перше слово), ознаки авто та позиція (перед/зад/ліво/право) з назви."""
    raw = TOKEN.findall((name or "").lower())
    bt = set(TOKEN.findall((brand or "").lower()))
    typ, veh, pos = "", set(), set()
    # (L)/(R) у дужках: сторона
    for m in re.findall(r"\(([lr])\)", (name or "").lower()):
        pos.add("L" if m == "l" else "P")
    for t in raw:
        if t in POSITION:
            pos.add(POSITION[t])
        elif t in STOP or t in bt or t.isdigit() or len(t) < 3 or re.fullmatch(r"\d+[a-zа-я]?", t):
            continue
        elif not typ and len(t) >= 4 and not any(c.isdigit() for c in t):
            typ = t
        else:
            veh.add(t)
    return {"type": typ, "veh": veh, "pos": pos}


def name_candidates(brand, sku_norm, name):
    """Кандидати в аналоги за назвою: той самий тип деталі, збіг ознак авто й позиції. Лише підказка (до 40 балів)."""
    me = name_profile(name, brand)
    if not me["type"] or len(me["veh"]) < 2:
        return {}
    key_tokens = sorted(me["veh"], key=len, reverse=True)[:2]
    cond = Q(name_lc__contains=me["type"][:6])
    for t in key_tokens:
        cond &= Q(name_lc__contains=t)
    out = {}
    for model, brand_of in ((SupplierItem, lambda o: o.brand), (Product, lambda o: o.brand.upper())):
        for o in model.objects.filter(cond)[:300]:
            b, n = brand_of(o), o.sku_norm
            if (b, n) == (brand, sku_norm) or n == sku_norm:
                continue
            pr = name_profile(o.name, b)
            if pr["type"][:6] != me["type"][:6] or pr["pos"] != me["pos"]:
                continue
            inter = me["veh"] & pr["veh"]
            if len(inter) < 2 or len(inter) / len(me["veh"]) < 0.6:
                continue
            sim = len(inter) / len(me["veh"] | pr["veh"])
            if sim > out.get((b, n), 0):
                out[(b, n)] = sim
    return out


# ---------------------------------------------------------------- детальний розрахунок для картки
def _part_names(norms):
    """{(бренд, артикул_норм): {name, sku}} за нормалізованими артикулами з усіх джерел."""
    parts = {}
    for p in Product.objects.filter(sku_norm__in=norms):
        parts[(p.brand.upper(), p.sku_norm)] = {"name": p.name, "sku": p.sku}
    for i in SupplierItem.objects.filter(sku_norm__in=norms):
        parts.setdefault((i.brand, i.sku_norm), {"name": i.name, "sku": i.sku})
    for o in PartOe.objects.filter(sku_norm__in=norms):
        parts.setdefault((o.brand, o.sku_norm), {"name": "", "sku": o.sku or o.sku_norm})
    return parts


def analogs(brand, sku_norm, limit=40, with_names=False):
    """Аналоги деталі: рівень, оцінка довіри 0-100, докази, залишок на складі та пропозиції постачальників."""
    brand = (brand or "").upper()
    me = (brand, sku_norm)
    rej = rejected(sku_norm)
    ev = {}                                          # (бренд, норм) -> список (вид, вага, текст)

    def add(key, kind, weight, text):
        if key == me or key[1] == sku_norm or key[1] in rej:
            return
        ev.setdefault(key, []).append((kind, weight, text))

    my_oes = {o.oe_norm: o.oe_raw for o in PartOe.objects.filter(brand=brand, sku_norm=sku_norm)}
    shared = {}
    if my_oes:
        for o in PartOe.objects.filter(oe_norm__in=list(my_oes)):
            shared.setdefault((o.brand, o.sku_norm), set()).add(my_oes[o.oe_norm])
    for key, s in shared.items():
        add(key, "oe", 70 + 10 * (min(len(s), 3) - 1), "Спільні OE: " + ", ".join(sorted(s)[:4]))

    direct = {}
    for a, b, src in CrossRef.objects.filter(Q(a=sku_norm) | Q(b=sku_norm)).values_list("a", "b", "source"):
        direct.setdefault(b if a == sku_norm else a, set()).add(src)
    hop2 = set()
    if direct:
        for a, b in CrossRef.objects.filter(Q(a__in=list(direct)) | Q(b__in=list(direct))).values_list("a", "b"):
            hop2.update((a, b))
    hop2 -= set(direct) | {sku_norm}
    parts = _part_names(set(direct) | hop2)
    srcname = {"manual": "Підтверджено вручну", "api": "Кросс із API постачальника", "import": "Кросс із файлу"}
    for n, srcs in direct.items():
        for k in [k for k in parts if k[1] == n] or [("", n)]:
            for s in srcs:
                kind = s if s in ("manual", "api") else "import"
                add(k, kind, WEIGHT[kind], srcname[kind])
    for n in hop2:
        for k in [k for k in parts if k[1] == n] or [("", n)]:
            add(k, "chain", WEIGHT["chain"], "Через ланцюжок кросів")

    names = _part_names({k[1] for k in ev} | {sku_norm})
    if with_names:
        mine = names.get(me, {}).get("name") or ""
        if not mine:
            p = Product.objects.filter(sku_norm=sku_norm, brand__iexact=brand).first()
            mine = p.name if p else ""
        for key, sim in name_candidates(brand, sku_norm, mine).items():
            add(key, "name", round(40 * sim), f"Збіг за назвою ({round(sim * 100)}%): той самий тип, авто й позиція")
        names = _part_names({k[1] for k in ev} | {sku_norm})

    rows = []
    for (b, n), items in ev.items():
        top = max(items, key=lambda x: x[1])
        kinds = {k for k, _, _ in items}
        score = min(100, top[1] + 5 * (len(kinds) - 1))
        lvl = KIND_LEVEL[top[0]]
        if "manual" in kinds:
            lvl, score = "manual", 100
        info = names.get((b, n), {})
        prods = Product.objects.filter(sku_norm=n, brand__iexact=b)
        prod = prods.first()
        offer = SupplierItem.objects.filter(sku_norm=n, brand=b, available=True).select_related("supplier").order_by("cost").first()
        rows.append({"brand": b, "sku": info.get("sku") or n, "sku_norm": n, "name": info.get("name") or (prod.name if prod else ""),
                     "level": lvl, "label": LABEL[lvl], "score": score, "evidence": [t for _, _, t in sorted(items, key=lambda x: -x[1])],
                     "shared": sorted(shared.get((b, n), [])), "stock": prods.aggregate(s=Sum("current_stock"))["s"] or 0,
                     "price": prod.price if prod else None, "offer_cost": offer.cost if offer else None,
                     "offer_sup": offer.supplier.name if offer else ""})
    rows.sort(key=lambda r: (-r["score"], -(r["stock"] > 0), r["offer_cost"] if r["offer_cost"] is not None else 10 ** 9))
    return rows[:limit]
