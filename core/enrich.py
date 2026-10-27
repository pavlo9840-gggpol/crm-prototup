"""Дані про деталь (OE-коди, опис, характеристики, застосовність, фото) з картки товару постачальника.
Прайс постачальника цих даних не містить, тому для кожної позиції робиться окремий запит до картки,
а результат зберігається у себе (PartOe / PartInfo) і далі використовується для точних аналогів."""
import json
import re
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from django.db import transaction
from django.db.models import Exists, F, OuterRef

from . import apisync
from .apisync import SyncError, _json_path
from .models import CrossRef, PartInfo, PartOe, SupplierItem, norm_sku

GUESS = {
    "oe": ("oe", "oe_codes", "oe_numbers", "oes", "oe_list", "oem", "oems", "oe_codes_full", "original_numbers"),
    "desc": ("description", "descr", "full_description", "short_description", "text", "opis"),
    "attrs": ("attributes", "characteristics", "properties", "specs", "specifications", "params", "parameters"),
    "cars": ("cars", "applicability", "vehicles", "fitment", "compatibility", "applications", "models"),
    "image": ("default_image", "image", "photo", "main_image", "images", "photos", "picture"),
    "analogs": ("analogs", "analog", "crosses", "cross", "cross_references", "replacements", "substitutes", "interchange"),
}
ANALOG_KEYS = ("article", "code", "number", "sku", "part_number", "num")
OE_CODE_KEYS = ("code", "number", "oe", "oe_number", "article", "num", "value")
OE_BRAND_KEYS = ("brand", "manufacturer", "maker", "make", "producer", "name_brand")


def _find(obj, names, depth=2):
    if isinstance(obj, dict):
        low = {str(k).lower(): v for k, v in obj.items()}
        for n in names:
            if low.get(n) not in (None, "", [], {}):
                return low[n]
        if depth > 0:
            for v in obj.values():
                if isinstance(v, dict):
                    r = _find(v, names, depth - 1)
                    if r is not None:
                        return r
    return None


def _pick(obj, path, guess):
    if path:
        v = _json_path(obj, path)
        return v if v not in (None, "", [], {}) else None
    return _find(obj, GUESS[guess])


def _txt(v):
    if v is None or isinstance(v, (dict, list, bool)):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(v))).strip()


def _split(s):
    return [x.strip() for x in re.split(r"[,;\n]+", s) if x.strip()]


def _oe_items(raw, cfg):
    out, seen = [], set()

    def add(code, brand=""):
        code, brand = _txt(code), _txt(brand).upper()[:60]
        n = norm_sku(code)
        if n and len(code) <= 80 and n not in seen:
            seen.add(n)
            out.append((code, n, brand))
    if isinstance(raw, str):
        for c in _split(raw):
            add(c)
    elif isinstance(raw, dict):                                   # {"BMW": ["123", "456"]} або {"BMW": "123, 456"}
        for b, v in raw.items():
            for c in (v if isinstance(v, list) else _split(str(v))):
                add(c if not isinstance(c, dict) else _find(c, OE_CODE_KEYS, 0), b)
    elif isinstance(raw, list):
        ck = (cfg.det_oe_code,) if cfg.det_oe_code else OE_CODE_KEYS
        bk = (cfg.det_oe_brand,) if cfg.det_oe_brand else OE_BRAND_KEYS
        for it in raw:
            if isinstance(it, dict):
                add(_find(it, ck, 0), _find(it, bk, 0) or "")
            else:
                add(it)
    return out


def _analogs(raw):
    """Список аналогів з картки постачальника: рядки або обʼєкти {article, brand}. Повертає [артикул, ...]."""
    out = []
    items = raw if isinstance(raw, list) else (_split(raw) if isinstance(raw, str) else [])
    for it in items:
        code = _txt(_find(it, ANALOG_KEYS, 0)) if isinstance(it, dict) else _txt(it)
        if code and norm_sku(code) and len(code) <= 80:
            out.append(code)
    return out


def _attrs_text(raw):
    lines = []
    if isinstance(raw, dict):
        lines = [f"{_txt(k)}: {_txt(v)}" for k, v in raw.items() if _txt(v)]
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                k = _find(it, ("name", "title", "key", "label", "attribute"), 0)
                v = _find(it, ("value", "val", "text", "values"), 0)
                if k and v is not None and _txt(v if not isinstance(v, list) else ", ".join(map(str, v))):
                    lines.append(f"{_txt(k)}: {_txt(v if not isinstance(v, list) else ', '.join(map(str, v)))}")
            elif _txt(it):
                lines.append(_txt(it))
    elif isinstance(raw, str):
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
    return "\n".join(lines[:120])


def _cars_text(raw):
    lines = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                lines.append(" ".join(_txt(v) for v in it.values() if _txt(v)))
            elif _txt(it):
                lines.append(_txt(it))
    elif isinstance(raw, dict):
        for k, v in raw.items():
            lines.append(f"{_txt(k)}: {', '.join(_txt(x) for x in v)}" if isinstance(v, list) else f"{_txt(k)} {_txt(v)}")
    elif isinstance(raw, str):
        lines = [x.strip() for x in re.split(r"[\n;]+", raw) if x.strip()]
    return "\n".join(lines[:150])


def _image(raw, base):
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        raw = _find(raw, ("url", "path", "src", "link"), 0)
    raw = _txt(raw)
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw[:400]
    return (base.rstrip("/") + "/" + raw.lstrip("/"))[:400] if base else ""


def extract_info(obj, cfg):
    return {
        "oe": _oe_items(_pick(obj, cfg.det_oe, "oe"), cfg),
        "description": _txt(_pick(obj, cfg.det_desc, "desc")),
        "attrs": _attrs_text(_pick(obj, cfg.det_attrs, "attrs")),
        "cars": _cars_text(_pick(obj, cfg.det_cars, "cars")),
        "image_url": _image(_pick(obj, cfg.det_image, "image"), cfg.det_image_base),
        "analogs": _analogs(_pick(obj, cfg.det_analogs, "analogs")),
    }


def describe_structure(obj, limit=70):
    """Перелік шляхів і прикладів значень відповіді: щоб знайти правильні шляхи для налаштування."""
    out = []

    def walk(o, path, depth):
        if len(out) >= limit:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else str(k), depth + 1)
        elif isinstance(o, list):
            out.append(f"{path}: список ({len(o)})")
            if o and depth < 5:
                walk(o[0], path + "[0]", depth + 1)
        else:
            out.append(f"{path} = {str(o)[:70]!r}")
    walk(obj, "", 0)
    return out


# ------------------------------------------------------------------ збереження
def save_info(brand, sku, info, source):
    brand, sn = (brand or "").upper(), norm_sku(sku)
    with transaction.atomic():
        pi, created = PartInfo.objects.get_or_create(brand=brand, sku_norm=sn, defaults={"sku": sku, "source": source})
        for f in ("description", "attrs", "cars", "image_url"):
            new = info.get(f) or ""
            if new and (created or pi.source != "manual" or not getattr(pi, f)):      # ручні правки мають пріоритет
                setattr(pi, f, new)
        if created:
            pi.source = source
        pi.save()
        if info.get("oe"):
            PartOe.objects.filter(brand=brand, sku_norm=sn, source=source).delete()
            have = set(PartOe.objects.filter(brand=brand, sku_norm=sn).values_list("oe_norm", flat=True))
            PartOe.objects.bulk_create([PartOe(brand=brand, sku_norm=sn, sku=sku, oe_norm=n, oe_raw=c, oe_brand=b, source=source)
                                        for c, n, b in info["oe"] if n not in have], ignore_conflicts=True)
    if info.get("analogs"):                                    # кроси від постачальника: джерело «api»
        mine, rows = sn, []
        for code in info["analogs"]:
            other = norm_sku(code)
            if other and other != mine:
                x, y = sorted((mine, other))
                rows.append(CrossRef(a=x, b=y, a_raw=sku if x == mine else code, b_raw=code if x == mine else sku, source="api"))
        CrossRef.objects.bulk_create(rows, ignore_conflicts=True)
    return pi


def card_request(cfg, ext_id):
    if not cfg.det_url or "{id}" not in cfg.det_url:
        raise SyncError("Не задано посилання на картку товару (воно має містити {id})")
    if not ext_id:
        raise SyncError("Для цієї позиції немає ID у постачальника (потрібне поле «ID товару» в налаштуваннях прайсу)")
    headers, params = apisync.auth_parts(cfg)
    u = urlparse(cfg.det_url.replace("{id}", quote(ext_id, safe="")))
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items()})
    return urlunparse(u._replace(query=urlencode(q))), headers


def fetch_card(cfg, ext_id, fetch=None):
    url, headers = card_request(cfg, ext_id)
    try:
        raw = (fetch or apisync.fetch_bytes)(url, headers)
        return json.loads(raw.decode("utf-8-sig"))
    except SyncError as e:
        raise SyncError(apisync.scrub(str(e), cfg))
    except (ValueError, UnicodeDecodeError):
        raise SyncError("Картка товару прийшла не у форматі JSON")


def enrich_item(si, fetch=None):
    cfg = getattr(si.supplier, "api", None)
    if cfg is None:
        raise SyncError("Для постачальника не налаштовано API")
    obj = fetch_card(cfg, si.ext_id, fetch)
    info = extract_info(obj, cfg)
    save_info(si.brand, si.sku, info, "api")
    return info


def enrich_batch(sup, limit=200, fetch=None, on_progress=None):
    """Підтягує дані для позицій, по яких їх ще немає (спершу ті, що вже є на складі чи продавались)."""
    cfg = getattr(sup, "api", None)
    if cfg is None or not cfg.det_url:
        raise SyncError("Не налаштовано посилання на картку товару")
    qs = (SupplierItem.objects.filter(supplier=sup, available=True).exclude(ext_id="")
          .annotate(has=Exists(PartInfo.objects.filter(brand=OuterRef("brand"), sku_norm=OuterRef("sku_norm"))))
          .filter(has=False).select_related("supplier__api").order_by(F("product_id").desc(nulls_last=True), "pk")[:limit])
    done = errors = streak = 0
    for si in qs:
        try:
            enrich_item(si, fetch)
            done += 1
            streak = 0
        except SyncError as e:
            if "429" in str(e):
                raise SyncError(f"Постачальник обмежив частоту запитів, оброблено {done}. Продовжте пізніше")
            errors += 1
            streak += 1
            if streak >= 20:
                raise SyncError(f"Забагато помилок підряд, зупинено. Остання: {e}")
            if "404" in str(e):                                   # картки немає: не питати щоразу
                PartInfo.objects.get_or_create(brand=si.brand, sku_norm=si.sku_norm, defaults={"sku": si.sku, "source": "api"})
        if on_progress:
            on_progress(done, errors)
    return done, errors
