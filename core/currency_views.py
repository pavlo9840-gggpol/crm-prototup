"""Сторінка «Курси валют»: курси, ручне введення, оновлення з НБУ."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from . import currency
from .models import Currency, ExchangeRate, Supplier
from .views import staff_only


@staff_only
def rates(request):
    if request.method == "POST":
        act = request.POST.get("act")
        try:
            if act == "nbu":
                messages.success(request, "Курси НБУ оновлено: " + currency.refresh_from_nbu(request.user))
            elif act == "manual":
                code = request.POST.get("code", "")
                try:
                    day = date.fromisoformat(request.POST.get("date") or "")
                except ValueError:
                    day = timezone.localdate()
                raw = (request.POST.get("rate") or "").replace(",", ".").strip()
                try:
                    rate = Decimal(raw)
                except Exception:
                    raise currency.RateError("Вкажіть курс числом, напр. 50.80")
                if day > timezone.localdate() + timedelta(days=1):
                    raise currency.RateError("Дата курсу не може бути в майбутньому")
                if not Currency.objects.filter(code=code, is_base=False).exists():
                    raise currency.RateError("Невідома валюта")
                changed, n = currency.save_rate(code, rate, day, "manual", request.user)
                messages.success(request, f"Курс {code} збережено: {rate.normalize():f}" + (f". Перераховано позицій у прайсах: {n}" if n else ""))
            elif act == "add_currency":
                code = (request.POST.get("code") or "").strip().upper()
                if not (len(code) == 3 and code.isalpha()):
                    raise currency.RateError("Код валюти: 3 латинські літери, напр. GBP")
                Currency.objects.get_or_create(code=code, defaults={"name": request.POST.get("name", "").strip() or code, "symbol": request.POST.get("symbol", "").strip()[:4]})
                messages.success(request, f"Валюту {code} додано: тепер задайте її курс")
            elif act == "recompute":
                total = 0
                for s in Supplier.objects.filter(currency__isnull=False):
                    total += currency.recompute_supplier_costs(s, request.user)[0]
                messages.success(request, f"Ціни в гривні перераховано за поточними курсами: {total} позицій")
        except currency.RateError as e:
            messages.error(request, str(e))
        return redirect("rates")
    late = currency.latest_rates()
    cur_rows = [{"c": c, "r": late.get(c.code), "stale": currency.is_stale(late.get(c.code)),
                 "suppliers": [s.name for s in Supplier.objects.filter(currency=c)]} for c in Currency.objects.filter(is_base=False)]
    history = ExchangeRate.objects.select_related("currency")[:40]
    return render(request, "core/rates.html", {"rows": cur_rows, "history": history, "today": timezone.localdate()})
