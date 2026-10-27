"""Товар під замовлення від постачальників (минає склад): прайси, аналоги, націнки, замовлення в дорозі,
історія продажів менеджерів."""
import csv
import io
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, IntegerField, Max, Q, Sum, Value, When
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import fitment, pricing, services
from .models import (CrossRef, Customer, CustomerCar, MarkupTier, PriceImport, Product, Sale, SaleItem, Supplier,
                     SupplierApi, SupplierItem, SupplierPriceChange, norm_sku)
from .crosses import analog_levels
from .services import StockError
from .views import D0, get_date, simple_form, staff_only

User = get_user_model()
MAX_FILE = 30_000_000
DEC = DecimalField(max_digits=16, decimal_places=2)


# ============================================================== читання Excel / CSV
class TableError(ValueError):
    pass


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_table(data, filename=""):
    """Повертає список рядків (список рядків-текстів) з .xlsx або .csv."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm") or data[:2] == b"PK":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            return [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
        except Exception as e:
            raise TableError(f"Не вдалося прочитати Excel-файл: {e}")
    if name.endswith(".xls"):
        raise TableError("Старий формат .xls не підтримується: збережіть файл як .xlsx або .csv")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise TableError("Невідоме кодування файлу (очікується UTF-8 або Windows-1251)")
    head = text[:4000]
    delim = max(";\t,", key=head.count)
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim)]


def num(v):
    s = str(v or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return Decimal(m.group())
    except InvalidOperation:
        return None


def qty_value(v):
    """None — наявність невідома (рахуємо, що товар є); 0 — немає."""
    s = str(v or "").strip().lower()
    if not s:
        return None
    if s in ("-", "—", "ні", "no") or s.startswith(("нема", "відсутн", "немає")):
        return Decimal(0)
    return num(s)


GUESS = {
    "sku": ("артикул", "каталожн", "номер", "код", "article", "sku", "part", "партномер"),
    "brand": ("бренд", "виробник", "марка", "brand", "manufactur", "producer"),
    "name": ("назва", "найменув", "опис", "name", "descr", "товар"),
    "cost": ("закуп", "ціна", "цена", "price", "cost", "вартість", "грн"),
    "qty": ("залишок", "наявн", "кількість", "кіл-", "склад", "qty", "stock", "остаток"),
}
FIELD_LABELS = [("sku", "Артикул *"), ("brand", "Бренд"), ("name", "Назва"), ("cost", "Закупівельна ціна *"),
                ("qty", "Наявність / залишок")]


def guess_columns(headers):
    out, used = {}, set()
    for field, keys in GUESS.items():
        for i, h in enumerate(headers):
            hl = h.lower()
            if i not in used and any(k in hl for k in keys):
                out[field] = i
                used.add(i)
                break
    return out


def _tmp_dir():
    d = Path(settings.MEDIA_ROOT) / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tmp_path(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise TableError("Файл імпорту втрачено, завантажте його заново")
    p = _tmp_dir() / f"{token}.bin"
    if not p.exists():
        raise TableError("Файл імпорту втрачено, завантажте його заново")
    return p


# ============================================================== імпорт прайсу постачальника
@staff_only
def supplier_prices(request):
    sups = list(Supplier.objects.select_related("api").annotate(n=Count("items", filter=Q(items__available=True)),
                                                                last=Max("imports__created_at")).order_by("name"))
    for s_ in sups:
        s_.cfg = getattr(s_, "api", None)
    return render(request, "core/supplier_prices.html", {"suppliers": sups,
                                                         "log": PriceImport.objects.select_related("supplier", "user")[:15]})


@staff_only
@require_POST
def price_map(request):
    """Крок 2: вибір колонок і попередній перегляд."""
    try:
        sup = get_object_or_404(Supplier, pk=request.POST.get("supplier") or 0)
        f = request.FILES.get("file")
        if f:
            if f.size > MAX_FILE:
                raise TableError("Файл завеликий (максимум 30 МБ)")
            token = uuid.uuid4().hex
            (_tmp_dir() / f"{token}.bin").write_bytes(f.read())
            fname = f.name
        else:
            token, fname = request.POST.get("token", ""), request.POST.get("fname", "")
        raw = _tmp_path(token).read_bytes()
        rows = read_table(raw, fname)
        hdr = max(1, int(request.POST.get("header_row") or 1))
        if len(rows) < hdr + 1:
            raise TableError("У файлі немає даних під рядком заголовка")
        headers = rows[hdr - 1]
        ncol = max(len(r) for r in rows[hdr - 1:hdr + 20])
        headers = headers + [""] * (ncol - len(headers))
        cols = guess_columns(headers)
        for fld, _ in FIELD_LABELS:                       # вибір користувача має перевагу над здогадкою
            v = request.POST.get(f"col_{fld}")
            if v not in (None, ""):
                cols[fld] = int(v)
            elif v == "" and f is None:
                cols.pop(fld, None)
    except TableError as e:
        messages.error(request, str(e))
        return redirect("supplier_prices")
    options = [{"i": i, "label": f"{chr(65 + i) if i < 26 else i + 1}: {h or '—'}"} for i, h in enumerate(headers)]
    fields = [{"key": k, "label": lbl, "sel": cols.get(k)} for k, lbl in FIELD_LABELS]
    preview = [r + [""] * (ncol - len(r)) for r in rows[hdr:hdr + 8]]
    from . import currency
    rate_info = None
    if currency.is_foreign(sup):
        try:
            rate_info = {"code": sup.currency.code, "rate": currency.effective_rate(sup)}
        except currency.RateError as e:
            rate_info = {"code": sup.currency.code, "error": str(e)}
    return render(request, "core/price_map.html", {"rate_info": rate_info,
        "sup": sup, "token": token, "fname": fname, "hdr": hdr, "options": options, "fields": fields,
        "headers": headers, "preview": preview, "total": len(rows) - hdr})


@staff_only
@require_POST
def price_apply(request):
    try:
        sup = get_object_or_404(Supplier, pk=request.POST.get("supplier") or 0)
        token, fname = request.POST.get("token", ""), request.POST.get("fname", "")
        path = _tmp_path(token)
        rows = read_table(path.read_bytes(), fname)
        hdr = max(1, int(request.POST.get("header_row") or 1))
        col = {k: int(request.POST[f"col_{k}"]) for k, _ in FIELD_LABELS if request.POST.get(f"col_{k}", "") != ""}
        if "sku" not in col or "cost" not in col:
            raise TableError("Оберіть колонки «Артикул» і «Закупівельна ціна»")
    except (TableError, ValueError) as e:
        messages.error(request, str(e))
        return redirect("supplier_prices")

    def get(r, k):
        i = col.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""

    parsed, bad = build_parsed({k: get(r, k) for k, _ in FIELD_LABELS} for r in rows[hdr:])
    if not parsed:
        messages.error(request, "У файлі не знайдено жодного коректного рядка (перевірте вибір колонок)")
        return redirect("supplier_prices")
    from .currency import RateError
    try:
        n_new, n_upd, n_gone = apply_parsed(sup, parsed, request.user, fname)
    except RateError as e:
        messages.error(request, str(e))
        return redirect("supplier_prices")
    try:
        path.unlink()
    except OSError:
        pass
    messages.success(request, f"Прайс «{sup.name}» оновлено: нових {n_new}, змінено {n_upd}, "
                              f"зникло з прайсу {n_gone}" + (f", пропущено некоректних рядків {bad}" if bad else ""))
    return redirect("supplier_prices")


def build_parsed(records):
    """records: словники {sku, brand, name, cost, qty} (рядки) → {(БРЕНД, артикул_норм): (sku, name, cost, qty)}."""
    parsed, bad = {}, 0
    for r in records:
        sku, cost = (r.get("sku") or "").strip(), num(r.get("cost"))
        if not sku or not norm_sku(sku) or cost is None or cost <= 0:
            bad += 1 if any(str(v or "").strip() for v in r.values()) else 0
            continue
        brand = " ".join((r.get("brand") or "").split()).upper()[:60]
        parsed[(brand, norm_sku(sku))] = (sku[:80], (r.get("name") or "").strip()[:250],
                                          cost.quantize(Decimal("0.01")), qty_value(r.get("qty")),
                                          (r.get("id") or "").strip()[:64])
    return parsed, bad


def apply_parsed(sup, parsed, user, fname):
    """Записує каталог постачальника: нові / змінені / зниклі позиції. Склад не змінюється."""
    now = timezone.now()
    from . import currency
    rate, foreign = currency.effective_rate(sup), currency.is_foreign(sup)        # RateError, якщо курсу немає
    with transaction.atomic():
        existing = {(i.brand, i.sku_norm): i for i in SupplierItem.objects.filter(supplier=sup)}
        new, upd, seen, changes = [], [], set(), []
        for (brand, sn), (sku, name, cost, q, ext) in parsed.items():
            it = existing.get((brand, sn))
            avail = q is None or q > 0
            orig = cost if foreign else None                    # ціна у валюті постачальника
            cost = currency.to_uah(orig, rate) if foreign else cost
            if it is None:
                new.append(SupplierItem(supplier=sup, brand=brand, sku=sku, sku_norm=sn, name=name, cost=cost, price_orig=orig,
                                        qty_avail=q, available=avail, ext_id=ext, name_lc=(name or "").lower()))
                continue
            seen.add(it.pk)
            if (it.cost, it.price_orig, it.qty_avail, it.available, it.name, it.ext_id) != (cost, orig, q, avail, name or it.name, ext or it.ext_id):
                if it.cost != cost:
                    changes.append((it, it.cost, cost))
                it.price_orig = orig
                it.cost, it.qty_avail, it.available, it.name, it.sku, it.updated_at = cost, q, avail, name or it.name, sku, now
                it.ext_id = ext or it.ext_id
                it.name_lc = (it.name or "").lower()
                upd.append(it)
        SupplierItem.objects.bulk_create(new, batch_size=500)
        SupplierItem.objects.bulk_update(upd, ["cost", "price_orig", "qty_avail", "available", "name", "sku", "updated_at", "ext_id", "name_lc"], batch_size=200)
        if changes:                                       # журнал змін цін + автоціна для товарів на складі
            SupplierPriceChange.objects.bulk_create([SupplierPriceChange(item=i, old_cost=o, new_cost=n) for i, o, n in changes], batch_size=500)
            from . import pricing
            pricing.reprice_after_supplier_change({(i.brand.upper(), i.sku_norm) for i, _, _ in changes}, user)
        gone = [i.pk for i in existing.values() if i.available and i.pk not in seen]
        for n in range(0, len(gone), 500):
            SupplierItem.objects.filter(pk__in=gone[n:n + 500]).update(available=False, qty_avail=Decimal(0))
        PriceImport.objects.create(supplier=sup, user=user, file_name=fname[:200], n_new=len(new),
                                   n_updated=len(upd), n_gone=len(gone))
    return len(new), len(upd), len(gone)


# ============================================================== аналоги (кроси)
def _read_pairs(rows, has_header):
    pairs = {}
    for r in rows[1 if has_header else 0:]:
        if len(r) < 2:
            continue
        a, b = norm_sku(r[0]), norm_sku(r[1])
        if a and b and a != b:
            x, y = sorted((a, b))
            pairs[(x, y)] = (r[0][:80], r[1][:80]) if a < b else (r[1][:80], r[0][:80])
    return pairs


@staff_only
def crossrefs(request):
    if request.method == "POST":
        act = request.POST.get("act")
        if act == "delete":
            CrossRef.objects.filter(pk=request.POST.get("pk") or 0).delete()
        elif act == "add":
            pairs = _read_pairs([[request.POST.get("a", ""), request.POST.get("b", "")]], False)
            if not pairs:
                messages.error(request, "Вкажіть два різні артикули")
            else:
                CrossRef.objects.bulk_create([CrossRef(a=a, b=b, a_raw=r[0], b_raw=r[1], source="manual") for (a, b), r in pairs.items()],
                                             ignore_conflicts=True)
                CrossRef.objects.filter(a__in=[p[0] for p in pairs], b__in=[p[1] for p in pairs]).update(source="manual")
                messages.success(request, "Аналог додано і позначено як підтверджений")
        elif act == "upload":
            f = request.FILES.get("file")
            try:
                if not f or f.size > MAX_FILE:
                    raise TableError("Оберіть файл .xlsx або .csv (до 30 МБ)")
                pairs = _read_pairs(read_table(f.read(), f.name), bool(request.POST.get("has_header")))
                before = CrossRef.objects.count()
                CrossRef.objects.bulk_create([CrossRef(a=a, b=b, a_raw=r[0], b_raw=r[1]) for (a, b), r in pairs.items()],
                                             ignore_conflicts=True, batch_size=500)
                messages.success(request, f"Імпортовано пар аналогів: {CrossRef.objects.count() - before} "
                                          f"(у файлі пар: {len(pairs)})")
            except TableError as e:
                messages.error(request, str(e))
        return redirect("crossrefs")
    q = norm_sku(request.GET.get("q", ""))
    qs = CrossRef.objects.filter(Q(a__startswith=q) | Q(b__startswith=q)) if q else CrossRef.objects.all()
    return render(request, "core/crossrefs.html", {"pairs": qs.order_by("-pk")[:100], "total": CrossRef.objects.count()})


# ============================================================== пошук на касі
@login_required
def pos_lookup(request):
    q = request.GET.get("q", "").strip()
    qn = norm_sku(q)
    if len(q) < 3 and len(qn) < 3:
        return JsonResponse({"analogs": [], "suppliers": []})
    levels = analog_levels(qn) if len(qn) >= 3 else {}
    an = set(levels)
    analogs = [{"id": p.pk, "n": p.name, "s": p.sku, "b": p.barcode or "", "p": float(p.price), "a": p.allow_price_change,
                "st": float(p.current_stock), "u": p.unit, "k": float(p.cost_price), "br": p.brand,
                "lv": levels.get(p.sku_norm, "")}
               for p in Product.objects.filter(is_active=True, current_stock__gt=0, sku_norm__in=an)[:10]] if an else []
    cond = Q(sku_norm=qn) | Q(sku_norm__in=an) | Q(sku_norm__startswith=qn)
    toks = [t for t in re.split(r"\s+", q) if len(t) >= 2]
    if len(q) >= 4 and toks:
        nm = Q()
        for t in toks:
            nm &= (Q(name_lc__contains=t.lower()) | Q(brand__icontains=t) | Q(sku__icontains=t))
        cond |= nm
    by = lambda *lv: [n for n, l in levels.items() if l in lv]
    qs = (SupplierItem.objects.filter(cond, available=True).select_related("supplier", "product")
          .annotate(rank=Case(When(sku_norm=qn, then=Value(0)), When(sku_norm__in=by("oe"), then=Value(1)),
                              When(sku_norm__in=by("manual", "cross"), then=Value(2)),
                              When(sku_norm__in=by("maybe"), then=Value(3)),
                              default=Value(4), output_field=IntegerField()))
          .order_by("rank", "cost")[:40])
    rs = pricing.RuleSet()
    sup = [{"si": i.pk, "brand": i.brand, "sku": i.sku, "name": i.name, "sup": i.supplier.name, "cost": float(i.cost),
            "avail": float(i.qty_avail) if i.qty_avail is not None else None,
            "kind": "exact" if i.rank == 0 else "analog" if i.rank <= 3 else "like",
            "lv": levels.get(i.sku_norm, ""),
            "rl": pricing.rule_json(rs.for_(i.brand, i.product.category_id if i.product_id else None)),
            "stock": float(i.product.current_stock) if i.product_id else 0} for i in qs]
    fit = _fit_checker(request.GET.get("car"), [(a["br"].upper(), norm_sku(a["s"])) for a in analogs] + [(i["brand"], norm_sku(i["sku"])) for i in sup])
    for a in analogs:
        a["fit"] = fit(a["br"].upper(), norm_sku(a["s"]))
    for i in sup:
        i["fit"] = fit(i["brand"], norm_sku(i["sku"]))
    return JsonResponse({"analogs": analogs, "suppliers": sup})


def _fit_checker(car_id, pairs):
    """Підказка «підходить до авто»: True/False за списком застосовності деталі; None, якщо даних немає або авто не вибрано.
    Лише підказка: назви авто в каталозі й у картці клієнта можуть бути записані по-різному."""
    car = CustomerCar.objects.filter(pk=car_id).first() if car_id and str(car_id).isdigit() else None
    toks = [x for x in re.findall(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+", f"{car.make} {car.model}".lower()) if len(x) >= 2] if car else []
    if not toks:
        return lambda b, n: None
    from .models import PartInfo
    info = {(p.brand, p.sku_norm): [ln.lower() for ln in p.cars.splitlines() if ln.strip()]
            for p in PartInfo.objects.filter(sku_norm__in=[n for _, n in pairs]).exclude(cars="")}
    return lambda b, n: (None if (b, n) not in info else any(fitment.line_matches(ln, toks, car.year) for ln in info[(b, n)]))


# ============================================================== націнки
class MarkupForm(forms.ModelForm):
    class Meta:
        model = MarkupTier
        fields = ["name", "percent", "is_default"]

    def save(self, commit=True):
        obj = super().save(commit)
        if commit and obj.is_default:
            MarkupTier.objects.exclude(pk=obj.pk).update(is_default=False)
        return obj


@staff_only
def markup_list(request):
    return render(request, "core/simple_list.html", {"title": "Націнки клієнтів", "items": MarkupTier.objects.all(),
                                                    "add": "markup_new", "edit": "markup_edit"})


@staff_only
def markup_edit(request, pk=None):
    return simple_form(request, MarkupForm, "Націнка", "markup_list", pk, MarkupTier)


# ============================================================== замовлення в дорозі
def _orders_qs(user):
    qs = (SaleItem.objects.filter(source=SaleItem.SUPPLIER, sale__status=Sale.DONE)
          .select_related("sale__customer", "sale__cashier", "sale__car", "supplier", "supplier_item", "product"))
    return qs if user.is_staff else qs.filter(sale__cashier=user)


@login_required
def orders(request):
    st = request.GET.get("st", "open")
    sup = request.GET.get("sup", "")
    qs = _orders_qs(request.user)
    today = timezone.localdate()
    if st == "ordered":
        qs = qs.filter(fulfil_status=SaleItem.ORDERED)
    elif st == "arrived":
        qs = qs.filter(fulfil_status=SaleItem.ARRIVED)
    elif st == "overdue":
        qs = qs.filter(fulfil_status=SaleItem.ARRIVED, pickup_deadline__lt=today)
    elif st == "closed":
        qs = qs.exclude(fulfil_status__in=[SaleItem.ORDERED, SaleItem.ARRIVED]).filter(
            sale__created_at__gte=timezone.now() - timedelta(days=60))
    else:
        qs = qs.filter(fulfil_status__in=[SaleItem.ORDERED, SaleItem.ARRIVED])
    if sup:
        qs = qs.filter(supplier_id=sup)
    items = list(qs.order_by("pickup_deadline", "sale__created_at")[:500])
    for i in items:
        i.left = i.quantity - i.returned_qty
        i.dl = i.days_left
        i.status_name = dict(SaleItem.FULFIL).get(i.fulfil_status, "")
    to_order = {}
    for i in items:
        if i.fulfil_status == SaleItem.ORDERED:
            si = i.supplier_item
            line = f"{(si.brand + ' ') if si and si.brand else ''}{si.sku if si else i.product.sku} × {i.left.normalize():f}"
            to_order.setdefault(i.supplier.name if i.supplier else "—", []).append(line)
    return render(request, "core/orders.html", {
        "items": items, "st": st, "sup": sup, "suppliers": Supplier.objects.all(),
        "to_order": {k: "\n".join(v) for k, v in to_order.items()}, "default_days": services.CompanySettings.get().pickup_days})


@login_required
@require_POST
def order_action(request, pk, action):
    item = get_object_or_404(_orders_qs(request.user), pk=pk)
    try:
        if action == "arrived":
            services.mark_arrived(request.user, item, request.POST.get("days"))
            messages.success(request, "Товар прибув, відлік на забір запущено")
        elif action == "issued":
            services.mark_issued(request.user, item)
            messages.success(request, "Видано клієнту (минаючи склад)")
        elif action == "stock":
            services.stock_unclaimed(request.user, item)
            messages.success(request, "Товар оприбутковано на склад, позицію в чеку скасовано")
        elif action == "supplier_return":
            services.return_to_supplier(request.user, item)
            messages.success(request, "Позначено як повернуто постачальнику, позицію в чеку скасовано")
        else:
            raise StockError("Невідома дія")
    except (StockError, ValueError) as e:
        messages.error(request, str(e))
    return redirect(request.POST.get("next") or "orders")


# ============================================================== історія продажів менеджерів
QTY_NET = ExpressionWrapper(F("quantity") - F("returned_qty"), output_field=DEC)
REV = ExpressionWrapper((F("quantity") - F("returned_qty")) * F("price"), output_field=DEC)
COST = ExpressionWrapper((F("quantity") - F("returned_qty")) * F("cost_price"), output_field=DEC)


@login_required
def manager_sales(request):
    today = timezone.localdate()
    d1 = get_date(request, "from", today.replace(day=1))
    d2 = get_date(request, "to", today)
    qs = (SaleItem.objects.filter(sale__status=Sale.DONE, sale__created_at__date__gte=d1, sale__created_at__date__lte=d2)
          .filter(returned_qty__lt=F("quantity")))
    mgr = request.GET.get("manager", "")
    if request.user.is_staff:
        if mgr:
            qs = qs.filter(sale__cashier_id=mgr)
    else:
        qs = qs.filter(sale__cashier=request.user)
    cq = request.GET.get("customer", "").strip()
    if cq:
        qs = qs.filter(Q(sale__customer__name_lc__contains=cq.lower()) | Q(sale__customer__phone__icontains=cq))
    src = request.GET.get("src", "")
    if src in (SaleItem.STOCK, SaleItem.SUPPLIER):
        qs = qs.filter(source=src)
    base = qs.annotate(l_rev=REV, l_cst=COST)
    tot = base.aggregate(s_rev=Sum("l_rev"), s_cst=Sum("l_cst"))
    rev, cst = tot["s_rev"] or D0, tot["s_cst"] or D0
    summary = []
    if request.user.is_staff:
        for r in (base.values("sale__cashier__username").annotate(rev=Sum("l_rev"), cst=Sum("l_cst"), n=Count("sale", distinct=True))
                  .order_by("-rev")):
            r["margin"] = r["rev"] - r["cst"]
            r["pct"] = (r["margin"] / r["rev"] * 100) if r["rev"] else D0
            summary.append(r)
    rows = list(base.select_related("sale__customer", "sale__cashier", "sale__car", "product", "supplier", "supplier_item")
                .order_by("-sale__created_at", "pk")[:2000])
    for r in rows:
        r.qn = r.quantity - r.returned_qty
        r.sum = r.qn * r.price
        r.mg = r.qn * (r.price - r.cost_price)
        r.mg_pct = ((r.price - r.cost_price) / r.price * 100) if r.price else D0
        r.status_name = dict(SaleItem.FULFIL).get(r.fulfil_status, "")
    if request.GET.get("csv"):
        resp = HttpResponse(content_type="text/csv; charset=utf-8-sig")
        resp["Content-Disposition"] = 'attachment; filename="manager_sales.csv"'
        w = csv.writer(resp, delimiter=";")
        w.writerow(["Дата", "Чек", "Менеджер", "Клієнт", "Телефон", "Товар", "Артикул", "К-сть", "Ціна",
                    "Закупівля", "Сума", "Маржа", "Маржа %", "Джерело", "Авто"])
        for r in rows:
            c = r.sale.customer
            w.writerow([timezone.localtime(r.sale.created_at).strftime("%d.%m.%Y %H:%M"), r.sale.number, r.sale.cashier,
                        c.name if c else "Роздрібний", c.phone if c else "", r.product.name, r.product.sku,
                        r.qn, r.price, r.cost_price, r.sum, r.mg, f"{r.mg_pct:.1f}",
                        "постачальник" if r.source == SaleItem.SUPPLIER else "склад", r.sale.car.title if r.sale.car_id else ""])
        return resp
    mypay = None
    if not request.user.is_staff:                      # менеджер бачить свою комісію за вибраний період
        from . import payroll
        pr_rows, _ = payroll.compute(d1, d2, only_user=request.user)
        mypay = pr_rows[0] if pr_rows and pr_rows[0]["plan"] else None
    return render(request, "core/manager_sales.html", {
        "mypay": mypay, "rows": rows, "summary": summary, "rev": rev, "cst": cst, "mg": rev - cst,
        "pct": ((rev - cst) / rev * 100) if rev else D0, "d1": d1, "d2": d2, "mgr": mgr, "cq": cq, "src": src,
        "managers": User.objects.filter(is_active=True).order_by("username") if request.user.is_staff else []})


# ============================================================== API постачальника
class SupplierApiForm(forms.ModelForm):
    api_key_new = forms.CharField(label="API-ключ", required=False, strip=True,
                                  widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
                                  help_text="Збережений ключ не показується. Залиште порожнім, щоб не змінювати. "
                                            "Можна вказати env:НАЗВА, тоді ключ береться зі змінної середовища сервера.")
    clear_key = forms.BooleanField(label="Видалити збережений ключ", required=False)

    class Meta:
        model = SupplierApi
        fields = ["url", "method", "body", "fmt", "auth_mode", "auth_name", "user_agent", "items_path", "header_row",
                  "f_id", "f_sku", "f_brand", "f_name", "f_cost", "f_qty", "page_param", "max_pages",
                  "det_url", "det_oe", "det_oe_code", "det_oe_brand", "det_desc", "det_attrs", "det_cars", "det_analogs",
                  "det_image", "det_image_base"]
        widgets = {"url": forms.TextInput(attrs={"style": "width:100%;max-width:520px", "placeholder": "https://api.постачальник.ua/v1/products"})}

    GROUPS = [("1. Підключення", ["url", "method", "body", "fmt", "auth_mode", "auth_name", "api_key_new", "clear_key", "user_agent"]),
              ("2. Як читати прайс і залишки", ["items_path", "header_row", "f_id", "f_sku", "f_brand", "f_name", "f_cost", "f_qty",
                                                 "page_param", "max_pages"]),
              ("3. Дані про деталь: OE-коди, опис, характеристики (необов'язково)",
               ["det_url", "det_oe", "det_oe_code", "det_oe_brand", "det_desc", "det_attrs", "det_cars", "det_analogs", "det_image",
                "det_image_base"])]

    def groups(self):
        return [(t, [self[f] for f in names]) for t, names in self.GROUPS]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        for f in ("method", "user_agent", "header_row"):      # мають значення за замовчуванням
            self.fields[f].required = False

    def clean_body(self):
        b = self.cleaned_data.get("body", "").strip()
        if b:
            try:
                json.loads(b.replace("{page}", "1"))
            except ValueError:
                raise forms.ValidationError("Тіло запиту має бути коректним JSON")
        return b

    def clean_url(self):
        u = self.cleaned_data["url"].strip()
        if not u.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Посилання має починатися з http:// або https://")
        return u

    def clean(self):
        d = super().clean()
        has_key = bool(d.get("api_key_new")) or (bool(self.instance.api_key) and not d.get("clear_key"))
        if d.get("auth_mode") and d["auth_mode"] != "none" and not has_key:
            self.add_error("api_key_new", "Вкажіть API-ключ або оберіть «Без ключа»")
        return d

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("clear_key"):
            obj.api_key = ""
        if self.cleaned_data.get("api_key_new"):
            obj.api_key = self.cleaned_data["api_key_new"]
        if commit:
            obj.save()
        return obj


@staff_only
def supplier_api(request, pk):
    from . import apisync
    sup = get_object_or_404(Supplier, pk=pk)
    cfg = SupplierApi.objects.filter(supplier=sup).first()
    form = SupplierApiForm(request.POST or None, instance=cfg or SupplierApi(supplier=sup))
    result = None
    if request.method == "POST":
        act = request.POST.get("act", "save")
        if act == "save":
            if form.is_valid():
                obj = form.save(commit=False)
                obj.supplier = sup
                obj.save()
                messages.success(request, "Налаштування API збережено")
                return redirect("supplier_api", pk=sup.pk)
        elif act == "preset":
            pr = apisync.PRESETS.get(request.POST.get("preset"))
            if not pr:
                messages.error(request, "Невідомий шаблон")
                return redirect("supplier_api", pk=sup.pk)
            obj = cfg or SupplierApi(supplier=sup)
            for k, v in pr["fields"].items():
                setattr(obj, k, v)
            obj.supplier = sup
            obj.save()
            messages.success(request, f"Застосовано шаблон «{pr['label']}». {pr['note']}")
            return redirect("supplier_api", pk=sup.pk)
        elif cfg is None:
            messages.error(request, "Спершу збережіть налаштування")
            return redirect("supplier_api", pk=sup.pk)
        else:
            sup = Supplier.objects.select_related("api").get(pk=sup.pk)
            try:
                if act == "test":
                    result = apisync.preview(sup)
                    messages.success(request, f"Зʼєднання працює: у першій відповіді {result['records']} записів, "
                                              f"з них коректних {result['valid']}. Нижче приклад того, як їх буде прочитано.")
                elif act == "probe":
                    from . import enrich
                    si = (SupplierItem.objects.filter(supplier=sup, available=True).exclude(ext_id="").order_by("pk").first())
                    if si is None:
                        raise apisync.SyncError("Спершу оновіть прайс так, щоб у позицій був ID постачальника (поле «ID товару»)")
                    obj = enrich.fetch_card(sup.api, si.ext_id)
                    got = enrich.extract_info(obj, sup.api)
                    result = {"probe": enrich.describe_structure(obj), "item": f"{si.brand} {si.sku}",
                              "got": {"oe": len(got["oe"]), "desc": bool(got["description"]), "attrs": bool(got["attrs"]),
                                      "cars": bool(got["cars"]), "image": bool(got["image_url"]), "analogs": len(got["analogs"])}}
                    messages.success(request, "Картку отримано. Нижче її структура і те, що вдалося розпізнати.")
                elif act == "enrich":
                    from . import enrich
                    done, errors = enrich.enrich_batch(sup, limit=200)
                    messages.success(request, f"Підтягнуто дані про деталі: {done}, помилок: {errors}. "
                                              f"Для решти позицій повторіть або запустіть команду enrich_parts за розкладом.")
                elif act == "sync":
                    messages.success(request, apisync.sync_supplier(sup, request.user, force=bool(request.POST.get("force"))))
                    if request.POST.get("next") == "supplier_prices":
                        return redirect("supplier_prices")
                    return redirect("supplier_api", pk=sup.pk)
            except apisync.SyncError as e:
                messages.error(request, str(e))
                if request.POST.get("next") == "supplier_prices":
                    return redirect("supplier_prices")
    return render(request, "core/supplier_api.html", {"sup": sup, "form": form, "cfg": cfg, "result": result,
                                                      "has_key": bool(cfg and cfg.api_key)})
