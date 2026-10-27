"""Екрани приймання накладної постачальника."""
import re
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import currency, receiving
from .models import CompanySettings, Currency, Receipt, Supplier
from .services import StockError
from .transit import MAX_FILE, TableError, _tmp_dir, _tmp_path, guess_columns, read_table

FIELDS = [("sku", "Артикул *"), ("brand", "Бренд"), ("qty", "Кількість *"), ("cost", "Ціна закупівлі"), ("name", "Назва")]


@login_required
def receive_home(request):
    qs = Receipt.objects.select_related("supplier", "user", "purchase")
    if not request.user.is_staff:
        qs = qs.filter(user=request.user)
    return render(request, "core/receive.html", {"suppliers": Supplier.objects.all(), "journal": qs[:20], "currencies": Currency.objects.all(),
                                                 "days": CompanySettings.get().pickup_days})


def _records(request, rows, hdr, cols):
    def get(r, k):
        i = cols.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""
    return [{k: get(r, k) for k, _ in FIELDS} for r in rows[hdr:]]


def _read(request):
    """Файл або вставлений текст → (token, ім'я, рядки таблиці)."""
    f = request.FILES.get("file")
    text = (request.POST.get("paste") or "").strip()
    if f:
        if f.size > MAX_FILE:
            raise TableError("Файл завеликий (максимум 30 МБ)")
        data, fname = f.read(), f.name
    elif text:
        data, fname = text.encode("utf-8"), "paste.csv"
    else:
        token, fname = request.POST.get("token", ""), request.POST.get("fname", "")
        return token, fname, read_table(_tmp_path(token).read_bytes(), fname)
    token = uuid.uuid4().hex
    (_tmp_dir() / f"{token}.bin").write_bytes(data)
    return token, fname, read_table(data, fname)


def _inv_rate(request, sup):
    """(код валюти накладної, курс). За замовчуванням валюта постачальника."""
    code = request.POST.get("inv_cur") or (sup.currency.code if currency.is_foreign(sup) else "UAH")
    return code, currency.invoice_rate(sup, code)


def _cols(request, rows, hdr, fresh):
    ncol = max((len(r) for r in rows[:30]), default=0)
    if hdr >= 1:
        headers = (rows[hdr - 1] + [""] * ncol)[:ncol]
        cols = guess_columns(headers)
    else:                                                       # без заголовка: Бренд; Артикул; Кількість; Ціна
        headers, cols = [""] * ncol, {"brand": 0, "sku": 1, "qty": 2, "cost": 3}
    for k, _ in FIELDS:
        v = request.POST.get(f"col_{k}")
        if v not in (None, "") and not fresh:
            cols[k] = int(v)
        elif v == "" and not fresh:
            cols.pop(k, None)
    return headers, cols, ncol


@login_required
@require_POST
def receive_preview(request):
    try:
        sup = get_object_or_404(Supplier, pk=request.POST.get("supplier") or 0)
        fresh = bool(request.FILES.get("file") or (request.POST.get("paste") or "").strip())
        token, fname, rows = _read(request)
        hdr = request.POST.get("header_row")
        hdr = int(hdr) if hdr not in (None, "") else (1 if request.FILES.get("file") else 0)   # вставлений текст зазвичай без заголовка
        hdr = max(0, hdr)
        headers, cols, ncol = _cols(request, rows, hdr, fresh)
        if "sku" not in cols or "qty" not in cols:
            raise TableError("Не вдалося визначити колонки «Артикул» і «Кількість»: оберіть їх вручну нижче")
        inv_cur, rate = _inv_rate(request, sup)
        lines, bad = receiving.parse_lines(_records(request, rows, hdr, cols), rate)
        pl = receiving.plan(sup, lines) if lines else {"rows": [], "totals": {"matched": 0, "stock_lines": 0, "errors": 0, "pending_after": 0}}
    except (TableError, ValueError, currency.RateError) as e:
        messages.error(request, str(e))
        return redirect("receive_home")
    options = [{"i": i, "label": f"{chr(65 + i) if i < 26 else i + 1}: {h or '—'}"} for i, h in enumerate(headers)]
    return render(request, "core/receive_preview.html", {
        "sup": sup, "token": token, "fname": fname, "hdr": hdr, "inv_cur": inv_cur, "rate": rate, "currencies": Currency.objects.all(), "number": request.POST.get("number", ""),
        "days": request.POST.get("days") or CompanySettings.get().pickup_days, "options": options,
        "fields": [{"key": k, "label": lbl, "sel": cols.get(k)} for k, lbl in FIELDS], "plan": pl, "bad": bad,
        "can_apply": bool(pl["totals"]["matched"] or pl["totals"]["stock_lines"])})


@login_required
@require_POST
@transaction.atomic
def receive_apply(request):
    try:
        # Серіалізуємо приймання по постачальнику, щоб два паралельні запити не пройшли перевірку одночасно.
        sup = get_object_or_404(Supplier.objects.select_for_update(), pk=request.POST.get("supplier") or 0)
        token, fname, rows = _read(request)
        hdr = max(0, int(request.POST.get("header_row") or 0))
        cols = {k: int(request.POST[f"col_{k}"]) for k, _ in FIELDS if request.POST.get(f"col_{k}", "") != ""}
        days = int(request.POST.get("days") or 0) or None
        if days is not None and not 1 <= days <= 365:
            raise ValueError("Кількість днів відліку: від 1 до 365")
        _code, rate = _inv_rate(request, sup)
        lines, _bad = receiving.parse_lines(_records(request, rows, hdr, cols), rate)
        # Захист від повторного оприбуткування тієї самої накладної новим завантаженням.
        # Порожній номер дозволяємо, оскільки частина постачальників його не надає.
        number = (request.POST.get("number") or "").strip()[:60]
        if number and Receipt.objects.filter(supplier=sup, number=number).exists():
            raise StockError(
                f"Накладну «{number}» від постачальника «{sup.name}» уже прийнято. "
                "Перевірте журнал приймань, щоб не оприбуткувати товар повторно."
            )
        rec = receiving.execute(request.user, sup, number, days, lines)
    except (TableError, ValueError, StockError, currency.RateError) as e:
        messages.error(request, str(e))
        return redirect("receive_home")
    try:
        _tmp_path(token).unlink()                                # повторне натискання вже не спрацює
    except (TableError, OSError):
        pass
    messages.success(request, f"Накладну прийнято: під клієнтів {rec.n_matched} поз., на склад {rec.n_stock} рядків"
                              + (f", пропущено {rec.n_skipped}" if rec.n_skipped else "") + ".")
    return redirect("receive_home")
