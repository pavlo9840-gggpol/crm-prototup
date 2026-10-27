"""Users management, database backups, product CSV import/export."""
import csv
import io
import re
import tempfile
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .models import Category, PriceHistory, Product, norm_sku
from .views import staff_only

User = get_user_model()
D0 = Decimal("0")


def superuser_only(view):
    @login_required
    @wraps(view)
    def wrapper(request, *a, **k):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *a, **k)
    return wrapper


# =============================================================== users
class UserForm(forms.Form):
    username = forms.CharField(label="Логін", max_length=150)
    first_name = forms.CharField(label="Ім'я (для відображення)", max_length=150, required=False)
    role = forms.ChoiceField(label="Роль", choices=[("cashier", "Касир (лише каса, свої продажі та зміни)"),
                                                    ("admin", "Адміністратор (повний доступ)")])
    is_active = forms.BooleanField(label="Активний (може входити)", required=False, initial=True)
    password = forms.CharField(label="Пароль", required=False, widget=forms.PasswordInput(render_value=False),
                               help_text="Мінімум 6 символів. Під час редагування залиште порожнім, щоб не змінювати.")

    def __init__(self, *a, user=None, editing_self=False, **k):
        super().__init__(*a, **k)
        self.user_obj, self.editing = user, user is not None
        if editing_self:
            self.fields["role"].disabled = True
            self.fields["is_active"].disabled = True
            self.fields["role"].help_text = "Свою роль і активність змінити не можна."

    def clean_username(self):
        u = self.cleaned_data["username"].strip()
        qs = User.objects.filter(username__iexact=u)
        if self.user_obj:
            qs = qs.exclude(pk=self.user_obj.pk)
        if qs.exists():
            raise forms.ValidationError("Такий логін уже існує")
        return u

    def clean(self):
        d = super().clean()
        pw = d.get("password", "")
        if not self.editing and not pw:
            self.add_error("password", "Вкажіть пароль")
        if pw and len(pw) < 6:
            self.add_error("password", "Пароль занадто короткий (мінімум 6 символів)")
        return d


def _active_admins(exclude=None):
    qs = User.objects.filter(is_superuser=True, is_active=True)
    return qs.exclude(pk=exclude.pk) if exclude else qs


@superuser_only
def user_list(request):
    return render(request, "core/user_list.html", {"users": User.objects.order_by("-is_superuser", "username")})


@superuser_only
def user_edit(request, pk=None):
    obj = get_object_or_404(User, pk=pk) if pk else None
    me = obj is not None and obj.pk == request.user.pk
    init = {"username": obj.username, "first_name": obj.first_name, "role": "admin" if obj.is_superuser else "cashier",
            "is_active": obj.is_active} if obj else {"role": "cashier", "is_active": True}
    form = UserForm(request.POST or None, initial=init, user=obj, editing_self=me)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if me:
            role, active = ("admin" if obj.is_superuser else "cashier"), obj.is_active
        else:
            role, active = d["role"], d["is_active"]
        if obj and obj.is_superuser and (role != "admin" or not active) and not _active_admins(exclude=obj).exists():
            form.add_error(None, "Не можна позбавити системи останнього активного адміністратора")
        else:
            u = obj or User()
            u.username, u.first_name = d["username"], d["first_name"]
            u.is_superuser = u.is_staff = (role == "admin")
            u.is_active = active
            if d["password"]:
                u.set_password(d["password"])
            elif not obj:
                u.set_unusable_password()
            u.save()
            messages.success(request, "Користувача збережено")
            return redirect("user_list")
    return render(request, "core/form.html", {"form": form, "title": "Користувач" if obj else "Новий користувач", "back": "user_list"})


# =============================================================== backups
BACKUP_DIR = Path(settings.BASE_DIR) / "backups"
BACKUP_NAME = re.compile(r"^backup-\d{8}-\d{6}\.zip$")


def make_backup_zip():
    """Consistent copy of the SQLite database (+ uploaded media) as ZIP bytes.
    Uses VACUUM INTO (an atomic snapshot that never waits on writers)."""
    if connection.vendor != "sqlite":
        raise RuntimeError("Автоматична резервна копія підтримується лише для SQLite")
    tmpdir = tempfile.mkdtemp()
    snap = Path(tmpdir) / "db.sqlite3"
    try:
        try:
            with connection.cursor() as cur:
                cur.execute("VACUUM INTO %s", [str(snap)])
        except Exception as e:
            raise RuntimeError(f"Не вдалося створити копію бази даних: {e}")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "db.sqlite3")
            media = Path(settings.MEDIA_ROOT)
            if media.exists():
                for f in media.rglob("*"):
                    if f.is_file():
                        z.write(f, "media/" + f.relative_to(media).as_posix())
            z.writestr("README.txt", "Відновлення: зупиніть сервер, замініть db.sqlite3 файлом з архіву, "
                                     "а папку media — вмістом media/ з архіву.\n")
        return buf.getvalue()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def save_backup(keep=14):
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"backup-{timezone.localtime():%Y%m%d-%H%M%S}.zip"
    path.write_bytes(make_backup_zip())
    old = sorted(p for p in BACKUP_DIR.glob("backup-*.zip") if BACKUP_NAME.match(p.name))
    for p in old[:-keep] if keep else []:
        p.unlink(missing_ok=True)
    return path


@superuser_only
def backup_page(request):
    if request.method == "POST":
        try:
            if request.POST.get("action") == "download":
                data = make_backup_zip()
                r = HttpResponse(data, content_type="application/zip")
                r["Content-Disposition"] = f'attachment; filename="backup-{timezone.localtime():%Y%m%d-%H%M%S}.zip"'
                return r
            p = save_backup()
            messages.success(request, f"Резервну копію збережено: {p.name}")
        except RuntimeError as e:
            messages.error(request, str(e))
        return redirect("backup")
    files = sorted((p for p in BACKUP_DIR.glob("backup-*.zip") if BACKUP_NAME.match(p.name)), reverse=True) if BACKUP_DIR.exists() else []
    return render(request, "core/backup.html", {"files": [{"name": p.name, "kb": p.stat().st_size // 1024} for p in files[:30]],
                                                "dir": str(BACKUP_DIR)})


@superuser_only
def backup_download(request, name):
    if not BACKUP_NAME.match(name) or not (BACKUP_DIR / name).is_file():
        raise Http404
    return FileResponse(open(BACKUP_DIR / name, "rb"), as_attachment=True, filename=name)


# =============================================================== products CSV
COLUMNS = [("sku", "Код"), ("name", "Назва"), ("barcode", "Штрих-код"), ("category", "Категорія"), ("unit", "Одиниця"),
           ("cost_price", "Ціна закупівлі"), ("price", "Ціна продажу"), ("min_stock", "Мін. залишок"),
           ("current_stock", "Залишок"), ("is_active", "Активний"), ("is_favorite", "Швидка кнопка"),
           ("allow_price_change", "Зміна ціни на касі"), ("brand", "Бренд")]
ALIASES = {}
for k, label in COLUMNS:
    ALIASES[k] = k
    ALIASES[label.lower()] = k
ALIASES.update({"sku": "sku", "артикул": "sku", "назва товару": "name", "штрихкод": "barcode", "barcode": "barcode", "ціна": "price",
                "ціна закупки": "cost_price", "одиниця виміру": "unit", "од.": "unit", "мінімум": "min_stock", "кількість": "current_stock", "виробник": "brand", "brand": "brand"})
BOOL_TRUE, BOOL_FALSE = {"1", "так", "yes", "true", "y", "+", "да"}, {"0", "ні", "no", "false", "n", "-", "нет"}
MAX_ROWS = 5000


def _dec(v):
    v = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    return Decimal(v)


def _bool(v):
    v = str(v).strip().lower()
    if v in BOOL_TRUE:
        return True
    if v in BOOL_FALSE:
        return False
    raise ValueError


def category_by_path(path):
    parent = None
    for part in [p.strip() for p in re.split(r"→|>|/|\\", path) if p.strip()]:
        parent, _ = Category.objects.get_or_create(name=part[:100], parent=parent)
    return parent


@staff_only
def product_export(request):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="products.csv"'
    resp.write("\ufeff")
    w = csv.writer(resp, delimiter=";")
    w.writerow([label for _, label in COLUMNS])
    if request.GET.get("template"):
        w.writerow(["COLA05", "Кола 0.5л", "4820000000011", "Напої → Газовані напої", "пляшка", "20.00", "30.00", "10", "100", "1", "1", "0", "SACHS"])
        return resp
    for p in Product.objects.select_related("category").order_by("name"):
        w.writerow([p.sku, p.name, p.barcode or "", str(p.category or ""), p.unit, f"{p.cost_price:.2f}", f"{p.price:.2f}",
                    f"{p.min_stock.normalize():f}", f"{p.current_stock.normalize():f}", int(p.is_active), int(p.is_favorite),
                    int(p.allow_price_change), p.brand])
    return resp


def parse_csv(raw):
    """-> (rows, errors, fatal). rows: list of dicts ready for apply (strings for JSON-session)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    first = text.splitlines()[0] if text.strip() else ""
    delim = max((";", ",", "\t"), key=first.count)
    rd = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = [h.strip().lower() for h in next(rd)]
    except StopIteration:
        return [], [], "Файл порожній"
    cols = {i: ALIASES[h] for i, h in enumerate(header) if h in ALIASES}
    if "sku" not in cols.values():
        return [], [], "Не знайдено колонки «Код» (SKU). Скачайте шаблон CSV та використайте його заголовки."
    rows, errors, seen_sku, seen_bc = [], [], set(), set()
    existing, by_norm = {}, {}
    for p in Product.objects.all():
        existing[(p.brand.upper(), p.sku_norm)] = p
        by_norm.setdefault(p.sku_norm, []).append(p)
    bc_owner = {p.barcode: ((p.brand.upper(), p.sku_norm), p.sku) for p in Product.objects.exclude(barcode__isnull=True)}
    for ln, rec in enumerate(rd, start=2):
        if not any(c.strip() for c in rec):
            continue
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append((ln, f"Перевищено ліміт {MAX_ROWS} рядків — решту пропущено"))
            break
        d = {cols[i]: rec[i].strip() for i in cols if i < len(rec)}
        sku = d.get("sku", "")
        try:
            norm = norm_sku(sku)
            if not sku or len(sku) > 50 or not norm:
                raise ValueError("порожній або задовгий код")
            brand = " ".join(d.get("brand", "").split()).upper()[:60]
            if brand:
                cur = existing.get((brand, norm))
            else:                       # бренд не вказано: шукаємо за артикулом, якщо він однозначний
                cands = by_norm.get(norm, [])
                if len(cands) > 1:
                    raise ValueError(f"артикул «{sku}» є в кількох брендів, вкажіть бренд")
                cur = cands[0] if cands else None
            ident = (cur.brand.upper(), cur.sku_norm) if cur else (brand, norm)
            if ident in seen_sku:
                raise ValueError(f"код «{sku}» повторюється у файлі")
            out = {"sku": sku, "line": ln, "new": cur is None, "pk": cur.pk if cur else None}
            if brand:
                out["brand"] = brand
            if cur is None and not d.get("name"):
                raise ValueError("для нового товару потрібна назва")
            if d.get("name"):
                out["name"] = d["name"][:200]
            for f in ("unit",):
                if d.get(f):
                    out[f] = d[f][:20]
            if "barcode" in d:
                bc = d["barcode"][:50] or None
                if bc:
                    if bc in seen_bc:
                        raise ValueError(f"штрих-код {bc} повторюється у файлі")
                    owner = bc_owner.get(bc)
                    if owner and owner[0] != ident:
                        raise ValueError(f"штрих-код {bc} вже належить товару {owner[1]}")
                    seen_bc.add(bc)
                out["barcode"] = bc
            if "category" in d and d["category"]:
                out["category"] = d["category"][:250]
            for f in ("cost_price", "price", "min_stock", "current_stock"):
                if d.get(f, "") != "":
                    try:
                        v = _dec(d[f])
                    except InvalidOperation:
                        raise ValueError(f"«{f}»: некоректне число «{d[f]}»")
                    if v < 0 or v > Decimal("99999999"):
                        raise ValueError(f"«{f}»: значення поза межами")
                    out[f] = str(v)
            for f in ("is_active", "is_favorite", "allow_price_change"):
                if d.get(f, "") != "":
                    try:
                        out[f] = _bool(d[f])
                    except ValueError:
                        raise ValueError(f"«{f}»: очікується 1/0 або так/ні")
            seen_sku.add(ident)
            rows.append(out)
        except ValueError as e:
            errors.append((ln, str(e)))
    return rows, errors, None


@transaction.atomic
def apply_rows(rows, user):
    created = updated = 0
    for r in rows:
        p = Product.objects.filter(pk=r["pk"]).first() if r.get("pk") else None
        is_new = p is None
        if is_new:
            p = Product(sku=r["sku"], name=r["name"])
        for f in ("name", "unit", "barcode", "brand"):
            if f in r:
                setattr(p, f, r[f])
        for f in ("is_active", "is_favorite", "allow_price_change"):
            if f in r:
                setattr(p, f, r[f])
        if "min_stock" in r:
            p.min_stock = Decimal(r["min_stock"])
        if "category" in r:
            p.category = category_by_path(r["category"])
        for f in ("price", "cost_price"):
            if f in r:
                new, old = Decimal(r[f]), getattr(p, f)
                setattr(p, f, new)
                if not is_new and old != new:
                    PriceHistory.objects.create(product=p, field=f, old_value=old, new_value=new, changed_by=user)
        if is_new:
            p.save()
            if Decimal(r.get("current_stock", "0")) > 0:
                services.add_movement(p, Decimal(r["current_stock"]), "opening", "", user, "Імпорт CSV")
            created += 1
        else:
            p.save()
            updated += 1
    return created, updated


@staff_only
def product_import(request):
    if request.method == "POST":
        f = request.FILES.get("file")
        if not f or f.size > 5_000_000:
            messages.error(request, "Оберіть CSV-файл (до 5 МБ)")
            return redirect("product_import")
        rows, errors, fatal = parse_csv(f.read())
        if fatal:
            messages.error(request, fatal)
            return redirect("product_import")
        request.session["product_import"] = rows
        return render(request, "core/product_import_preview.html", {
            "rows": rows[:200], "total": len(rows), "new": sum(1 for r in rows if r["new"]),
            "upd": sum(1 for r in rows if not r["new"]), "errors": errors[:200], "n_err": len(errors)})
    return render(request, "core/product_import.html", {"columns": COLUMNS})


@staff_only
@require_POST
def product_import_apply(request):
    rows = request.session.pop("product_import", None)
    if not rows:
        messages.error(request, "Немає даних для імпорту — завантажте файл заново")
        return redirect("product_import")
    try:
        c, u = apply_rows(rows, request.user)
    except Exception as e:  # whole import is atomic: nothing is saved on failure
        messages.error(request, f"Імпорт скасовано, жодних змін не збережено: {e}")
        return redirect("product_import")
    messages.success(request, f"Імпорт завершено: створено {c}, оновлено {u}")
    return redirect("product_list")
