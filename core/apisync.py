"""Підтягування каталогу постачальника через API / фід (JSON, XML, CSV/XLSX) у «Прайси постачальників».

Навмисні запобіжники:
  * ключ ніколи не потрапляє в тексти помилок і журнали;
  * адреси на внутрішню мережу заборонені (захист від SSRF), якщо не ввімкнено API_ALLOW_PRIVATE_HOSTS;
  * порожня або «підозріло мала» відповідь не стирає наявний каталог (потрібен явний force).
"""
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.conf import settings
from django.utils import timezone

from .models import SupplierItem

MAX_BYTES = 150_000_000
TIMEOUT = 60


class SyncError(Exception):
    pass


# ------------------------------------------------------------------ мережа
def check_url(url):
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise SyncError("Посилання має починатися з http:// або https://")
    if getattr(settings, "API_ALLOW_PRIVATE_HOSTS", False):
        return
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except OSError:
        raise SyncError("Не вдалося знайти адресу сервера. Перевірте посилання")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SyncError("Адреси у внутрішній мережі заборонені з міркувань безпеки")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url, headers, body=None):
    """Єдина точка мережевого виклику (у тестах підміняється). body != None → POST із цим тілом."""
    check_url(url)
    req = urllib.request.Request(url, data=body.encode("utf-8") if body is not None else None, headers=headers,
                                 method="POST" if body is not None else "GET")
    opener = urllib.request.build_opener(_SafeRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            buf, size = [], 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SyncError("Відповідь завелика (понад 150 МБ)")
                buf.append(chunk)
            return b"".join(buf)
    except urllib.error.HTTPError as e:
        hint = {401: "ключ відхилено", 403: "доступ заборонено (перевірте ключ і права)", 404: "адресу не знайдено",
                429: "перевищено ліміт запитів, спробуйте пізніше"}.get(e.code, "помилка сервера постачальника")
        raise SyncError(f"Постачальник відповів HTTP {e.code}: {hint}")
    except urllib.error.URLError as e:
        raise SyncError(f"Немає зʼєднання з постачальником: {getattr(e, 'reason', 'помилка мережі')}")
    except (TimeoutError, socket.timeout):
        raise SyncError("Постачальник не відповів за відведений час")


def get_key(cfg):
    k = cfg.api_key or ""
    if k.startswith("env:"):                       # ключ можна не зберігати в базі, а брати зі змінної середовища
        return os.environ.get(k[4:].strip(), "")
    return k


def auth_parts(cfg):
    """Заголовки й параметри авторизації (однакові для прайсу й картки товару)."""
    key = get_key(cfg)
    headers = {"Accept": "application/json, application/xml, text/csv, */*", "User-Agent": cfg.user_agent or "RetailShop/1.0"}
    params = {}
    if cfg.auth_mode != "none" and not key:
        raise SyncError("API-ключ не заданий")
    if cfg.auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif cfg.auth_mode == "header":
        headers[cfg.auth_name or "X-API-Key"] = key
    elif cfg.auth_mode == "query":
        params[cfg.auth_name or "api_key"] = key
    return headers, params


def build_request(cfg, page=None):
    headers, params = auth_parts(cfg)
    if page is not None and cfg.page_param:
        params[cfg.page_param] = page
    u = urlparse(cfg.url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items()})
    body = None
    if cfg.method == "POST":
        body = (cfg.body or "{}").replace("{page}", str(page or 1))
        if "ВСТАВТЕ" in body:
            raise SyncError("У тілі запиту лишились підказки «ВСТАВТЕ…»: впишіть UUID валюти та складів з вашого кабінету/документації")
        headers["Content-Type"] = "application/json"
    return urlunparse(u._replace(query=urlencode(q))), headers, body


def scrub(text, cfg):
    k = get_key(cfg)
    return text.replace(k, "***") if k else text


# ------------------------------------------------------------------ розбір відповідей
def _s(v):
    if v is None or isinstance(v, (dict, list, bool)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _json_path(obj, path):
    for part in [p for p in path.replace("/", ".").split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


FIELDS = ("sku", "brand", "name", "cost", "qty", "id")


def _rec(getter, cfg):
    return {k: _s(getter(getattr(cfg, f"f_{k}"))) if getattr(cfg, f"f_{k}") else "" for k in FIELDS}


def parse_json(data, cfg):
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise SyncError("Відповідь не є коректним JSON (можливо, невірний ключ або адреса)")
    items = _json_path(obj, cfg.items_path) if cfg.items_path else obj
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        raise SyncError(f"За шляхом «{cfg.items_path or '(корінь)'}» немає списку товарів. Перевірте поле «Де лежить список товарів»")
    return [_rec(lambda f, it=it: _json_path(it, f), cfg) for it in items if isinstance(it, dict)]


def _xml_val(el, path):
    cur = el
    parts = [p for p in path.replace(".", "/").split("/") if p]
    for i, part in enumerate(parts):
        tag, _, attr = part.partition("@")
        if tag:
            cur = cur.find(tag)
            if cur is None:
                return None
        if attr:
            return cur.get(attr)
    return cur.text


def parse_xml(data, cfg):
    if b"<!ENTITY" in data[:100_000]:
        raise SyncError("XML з оголошеннями ENTITY не приймається з міркувань безпеки")
    if not cfg.items_path:
        raise SyncError("Для XML вкажіть назву тегу однієї позиції (напр. offer)")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise SyncError("Відповідь не є коректним XML (можливо, невірний ключ або адреса)")
    return [_rec(lambda f, e=e: _xml_val(e, f), cfg) for e in root.iter(cfg.items_path)]


def parse_table(data, cfg):
    from .transit import TableError, num, read_table
    try:
        rows = read_table(data, "feed.xlsx" if data[:2] == b"PK" else "feed.csv")
    except TableError as e:
        raise SyncError(str(e))
    hr = max(1, cfg.header_row or 1)
    if len(rows) <= hr:
        raise SyncError("У таблиці немає даних")
    head = [h.strip().lower() for h in rows[hr - 1]]

    def cols(spec):
        out = []
        for part in (spec or "").split(","):
            n = part.strip().lower()
            if not n:
                continue
            if n.endswith("*"):                                   # QTY:* = усі колонки, що починаються на «qty:»
                out += [i for i, h in enumerate(head) if h.startswith(n[:-1])]
            elif n in head:
                out.append(head.index(n))
            elif len(n) == 1 and n.isalpha() and ord(n) - 97 < len(head):      # літера колонки: A, B, C…
                out.append(ord(n) - 97)
        return out
    idx = {k: cols(getattr(cfg, f"f_{k}")) for k in FIELDS}
    out = []
    for r in rows[hr:]:
        rec = {}
        for k in FIELDS:
            ix = idx[k]
            if not ix:
                rec[k] = ""
            elif k == "qty" and len(ix) > 1:                      # кілька складів: сума залишків
                rec[k] = str(sum((num(r[i]) or 0) for i in ix if i < len(r)))
            else:
                rec[k] = _s(r[ix[0]]) if ix[0] < len(r) else ""
        out.append(rec)
    return out


PARSERS = {"json": parse_json, "xml": parse_xml, "csv": parse_table}


def fetch_records(cfg, fetch=None, only_first_page=False):
    fetch = fetch or fetch_bytes            # обирається під час виклику, а не оголошення
    parse = PARSERS[cfg.fmt]
    records, prev = [], None
    pages = [None] if not cfg.page_param else range(1, (1 if only_first_page else cfg.max_pages) + 1)
    for page in pages:
        url, headers, body = build_request(cfg, page)
        try:
            raw = fetch(url, headers, body) if body is not None else fetch(url, headers)
            chunk = parse(raw, cfg)
        except SyncError as e:
            raise SyncError(scrub(str(e), cfg))
        if not chunk:
            break
        sig = json.dumps(chunk[0], sort_keys=True)
        if sig == prev:                       # API ігнорує номер сторінки і віддає те саме: зупиняємось
            break
        prev = sig
        records.extend(chunk)
    return records


# ------------------------------------------------------------------ синхронізація
def sync_supplier(sup, user=None, force=False, fetch=None):
    from .transit import apply_parsed, build_parsed
    cfg = getattr(sup, "api", None)
    if cfg is None or not cfg.url:
        raise SyncError("Для цього постачальника API не налаштовано")
    try:
        records = fetch_records(cfg, fetch)
        parsed, bad = build_parsed(records)
        if not parsed:
            raise SyncError("API не повернуло жодної коректної позиції (перевірте поля артикула й ціни). Каталог не змінено")
        avail = SupplierItem.objects.filter(supplier=sup, available=True).count()
        if not force and avail >= 50 and len(parsed) < avail * 0.5:
            raise SyncError(f"У відповіді {len(parsed)} позицій, а в каталозі зараз {avail}. Це підозріло мало, тому "
                            f"каталог не змінено. Якщо так і має бути, повторіть із позначкою «Оновити попри суттєве скорочення»")
        from .currency import RateError
        try:
            n_new, n_upd, n_gone = apply_parsed(sup, parsed, user, "API")
        except RateError as e:
            raise SyncError(str(e))
    except SyncError as e:
        cfg.last_result = f"{timezone.localtime():%d.%m.%Y %H:%M} · помилка: {e}"[:500]
        cfg.save(update_fields=["last_result"])
        raise
    cfg.last_sync_at = timezone.now()
    cfg.last_result = (f"{timezone.localtime():%d.%m.%Y %H:%M} · отримано {len(parsed)}: нових {n_new}, змінено {n_upd}, "
                       f"зникло {n_gone}" + (f", пропущено некоректних {bad}" if bad else ""))[:500]
    cfg.save(update_fields=["last_sync_at", "last_result"])
    return cfg.last_result


def preview(sup, fetch=None, limit=5):
    """Пробний запит: перша сторінка, без запису в базу."""
    from .transit import build_parsed
    cfg = sup.api
    records = fetch_records(cfg, fetch, only_first_page=True)
    parsed, bad = build_parsed(records)
    sample = [{"brand": b, "sku": v[0], "name": v[1], "cost": v[2], "qty": v[3]} for (b, _), v in list(parsed.items())[:limit]]
    return {"records": len(records), "valid": len(parsed), "bad": bad, "sample": sample}


# ------------------------------------------------------------------ шаблони постачальників
PRESETS = {
    "bmparts": {
        "label": "BM Parts (Busmarket)",
        "note": "Складено за публічною документацією developer.bm.parts. У тілі запиту впишіть UUID валюти та складів "
                "(є в документації й кабінеті). Токен видає менеджер BM Parts.",
        "fields": dict(url="https://api.bm.parts/prices/list", fmt="csv", method="POST", auth_mode="header",
                       auth_name="Authorization", user_agent="RetailShop/1.0", header_row=2, page_param="", items_path="",
                       body='{"currency": "ВСТАВТЕ_UUID_ВАЛЮТИ", "warehouses": ["ВСТАВТЕ_UUID_СКЛАДУ"]}',
                       f_id="UUID", f_sku="Article", f_brand="Brand", f_name="Name", f_cost="Price", f_qty="QTY:*",
                       det_url="https://api.bm.parts/product/{id}?oe=full"),
    },
}
