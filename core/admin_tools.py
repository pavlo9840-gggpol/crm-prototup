"""Users management, database backups, product CSV import/export."""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timedelta
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

from . import pricing, services
from .models import Category, StaffMembership, LicenseWorkstationSession, PriceHistory, Product, norm_sku
from .tenant_context import company_required

User = get_user_model()
D0 = Decimal("0")


def local_admin_only(view):
    """Allow the local store owner/admin; global superusers remain recovery admins."""
    @login_required
    @company_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        membership = getattr(request, "staff_membership", None)
        if not request.user.is_superuser and not (
            membership and membership.is_active and membership.role in ("owner", "admin")
        ):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


# =============================================================== users
class UserForm(forms.Form):
    username = forms.CharField(label="Логін", max_length=150)
    first_name = forms.CharField(label="Ім'я (для відображення)", max_length=150, required=False)
    role = forms.ChoiceField(label="Роль", choices=[("cashier", "Касир (лише каса, свої продажі та зміни)"),
                                                    ("manager", "Менеджер"), ("mechanic", "Механік"),
                                                    ("admin", "Адміністратор магазину"), ("owner", "Власник магазину")])
    is_active = forms.BooleanField(label="Активний (може входити)", required=False, initial=True)
    password = forms.CharField(label="Пароль", required=False, widget=forms.PasswordInput(render_value=False),
                               help_text="Мінімум 6 символів. Під час редагування залиште порожнім, щоб не змінювати.")

    def __init__(self, *a, user=None, editing_self=False, editing_owner=False, **k):
        super().__init__(*a, **k)
        self.user_obj, self.editing = user, user is not None
        if not editing_owner:
            self.fields["role"].choices = [choice for choice in self.fields["role"].choices if choice[0] != "owner"]
        if editing_self or editing_owner:
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


@local_admin_only
def user_list(request):
    memberships = StaffMembership.objects.filter(company=request.company).select_related("user").order_by(
        "-role", "user__username"
    )
    return render(request, "core/user_list.html", {"memberships": memberships})


@local_admin_only
def user_edit(request, pk=None):
    membership = get_object_or_404(
        StaffMembership.objects.select_related("user").filter(company=request.company), user_id=pk
    ) if pk else None
    obj = membership.user if membership else None
    me = obj is not None and obj.pk == request.user.pk
    init = {
        "username": obj.username,
        "first_name": obj.first_name,
        "role": membership.role,
        "is_active": membership.is_active,
    } if membership else {"role": "cashier", "is_active": True}
    form = UserForm(request.POST or None, initial=init, user=obj, editing_self=me,
                    editing_owner=bool(membership and membership.role == "owner"))
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if me:
            role, active = membership.role, membership.is_active
        else:
            role, active = d["role"], d["is_active"]
        from .licensing import get_active_license
        active_license = get_active_license()
        active_members = StaffMembership.objects.filter(company=request.company, is_active=True).count()
        is_new_active_member = obj is None and active
        is_reactivating_member = bool(membership and not membership.is_active and active)
        if active_license and active_members >= active_license["max_users"] and (
            is_new_active_member or is_reactivating_member
        ):
            messages.error(
                request,
                f"Досягнуто ліміт ліцензії: {active_license['max_users']} активних користувачів."
            )
            return redirect("user_list")
        if obj is None:
            obj = User.objects.create_user(
                username=d["username"], password=d["password"], first_name=d["first_name"],
                is_staff=False, is_superuser=False,
            )
            StaffMembership.objects.create(company=request.company, user=obj, role=role, is_active=active)
        else:
            obj.username, obj.first_name = d["username"], d["first_name"]
            if d["password"]:
                obj.set_password(d["password"])
            obj.save(update_fields=["username", "first_name", "password"])
            membership.role, membership.is_active = role, active
            membership.save(update_fields=["role", "is_active"])
            if not active:
                LicenseWorkstationSession.objects.filter(user=obj).delete()
        messages.success(request, "Учасника магазину збережено")
        return redirect("user_list")
    return render(request, "core/form.html", {
        "form": form, "title": "Учасник магазину" if obj else "Новий учасник магазину", "back": "user_list"
    })


# =============================================================== backups
BACKUP_DIR = Path(os.environ.get("AUTOZAPAZ_BACKUP_DIR", Path(settings.AUTOZAPAZ_DATA_DIR) / "backups")).expanduser().resolve()
BACKUP_NAME = re.compile(r"^backup-\d{8}-\d{6}\.zip$")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup_archive(source):
    """Validate ZIP structure, optional manifest hashes and SQLite integrity.

    Accepts a filesystem path or ZIP bytes. Legacy archives without a manifest
    remain verifiable for backwards compatibility.
    """
    archive_source = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    try:
        with zipfile.ZipFile(archive_source, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Архів містить повторювані імена файлів.")
            for name in names:
                member = Path(name)
                if name.startswith(("/", "\\")) or "\\" in name or ".." in member.parts:
                    raise ValueError(f"Небезпечний шлях у ZIP-архіві: {name}")
                mode = (archive.getinfo(name).external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode):
                    raise ValueError(f"Символічні посилання в ZIP-архіві заборонені: {name}")
            if "db.sqlite3" not in names:
                raise ValueError("В архіві немає обов'язкового файлу db.sqlite3.")
            if archive.testzip() is not None:
                raise ValueError("ZIP-архів пошкоджений.")

            if "manifest.json" in names:
                try:
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
                        raise ValueError("Непідтримувана версія маніфесту резервної копії.")
                    if manifest.get("database_engine") != "sqlite":
                        raise ValueError("Маніфест містить невідому СУБД.")
                    expected = manifest.get("files", {})
                    if not isinstance(expected, dict) or "db.sqlite3" not in expected:
                        raise ValueError("Маніфест не містить контрольної суми db.sqlite3.")
                    payload_names = {
                        name for name in names
                        if name not in ("manifest.json", "README.txt") and not name.endswith("/")
                    }
                    if payload_names != set(expected):
                        missing = sorted(payload_names - set(expected))
                        absent = sorted(set(expected) - payload_names)
                        raise ValueError(
                            "Склад файлів не збігається з маніфестом "
                            f"(без контрольної суми: {missing}; відсутні в архіві: {absent})."
                        )
                    for name, metadata in expected.items():
                        if not isinstance(metadata, dict):
                            raise ValueError(f"Метадані файлу в маніфесті некоректні: {name}")
                        if not isinstance(metadata.get("size"), int) or metadata["size"] < 0:
                            raise ValueError(f"Розмір файлу в маніфесті некоректний: {name}")
                        digest = metadata.get("sha256")
                        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                            raise ValueError(f"SHA-256 у маніфесті некоректний: {name}")
                        info = archive.getinfo(name)
                        if info.file_size != metadata["size"]:
                            raise ValueError(f"Розмір файлу не збігається з маніфестом: {name}")
                        actual = hashlib.sha256(archive.read(name)).hexdigest()
                        if actual != digest:
                            raise ValueError(f"Контрольна сума не збігається: {name}")
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"Маніфест резервної копії пошкоджений: {exc}") from exc

            with tempfile.TemporaryDirectory(prefix="autozapaz-verify-") as temp_dir:
                database_path = Path(temp_dir) / "db.sqlite3"
                with archive.open("db.sqlite3") as zipped_db, open(database_path, "wb") as output:
                    shutil.copyfileobj(zipped_db, output)
                database = sqlite3.connect(str(database_path))
                try:
                    result = database.execute("PRAGMA integrity_check").fetchone()
                except sqlite3.DatabaseError as exc:
                    raise ValueError(f"Файл db.sqlite3 не є коректною SQLite-базою: {exc}") from exc
                finally:
                    database.close()
                if not result or result[0] != "ok":
                    raise ValueError(f"SQLite integrity_check не пройшов: {result[0] if result else 'немає відповіді'}")
            return {
                "database": "db.sqlite3",
                "media_files": sum(1 for name in names if name.startswith("media/") and not name.endswith("/")),
                "manifest": "manifest.json" in names,
                "integrity": "ok",
                "files": len(names),
            }
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError(f"Не вдалося прочитати резервну копію: {exc}") from exc


def restore_backup_archive(source, target_dir):
    """Restore a verified archive into a brand-new isolated directory.

    Existing destinations are never overwritten. This helper deliberately
    does not replace the live Django database or MEDIA_ROOT.
    """
    requested_target = Path(target_dir).expanduser()
    if requested_target.exists() or requested_target.is_symlink():
        raise ValueError(f"Каталог призначення вже існує; нічого не перезаписано: {requested_target}")
    target = requested_target.resolve()
    if target.exists():
        raise ValueError(f"Каталог призначення вже існує; нічого не перезаписано: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    verify_backup_archive(source)

    archive_source = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    temporary_root = Path(tempfile.mkdtemp(prefix=".autozapaz-restore-", dir=target.parent))
    try:
        with zipfile.ZipFile(archive_source, "r") as archive:
            for info in archive.infolist():
                name = info.filename
                if name in ("db.sqlite3", "manifest.json", "README.txt"):
                    if name != "db.sqlite3":
                        continue
                    destination = temporary_root / "db.sqlite3"
                elif name.startswith("media/"):
                    relative = Path(*Path(name).parts[1:])
                    if not relative.parts:
                        continue
                    destination = temporary_root / "media" / relative
                else:
                    raise ValueError(f"Непідтримуваний файл у резервній копії: {name}")

                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as zipped_file, open(destination, "wb") as output:
                    shutil.copyfileobj(zipped_file, output)

        if not (temporary_root / "db.sqlite3").is_file():
            raise ValueError("Після відновлення не знайдено db.sqlite3.")
        validation_archive = _zip_directory_for_validation(temporary_root)
        try:
            verify_backup_archive(validation_archive)
        finally:
            validation_archive.close()
        if target.exists():
            raise ValueError(f"Каталог призначення з'явився під час відновлення; нічого не перезаписано: {target}")
        temporary_root.rename(target)
        temporary_root = None
        return {
            "directory": target,
            "database": target / "db.sqlite3",
            "media": target / "media",
            "media_files": sum(1 for path in (target / "media").rglob("*") if path.is_file())
            if (target / "media").exists() else 0,
        }
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _zip_directory_for_validation(directory):
    """Build an in-memory ZIP from the extracted DB for a second SQLite check."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(Path(directory) / "db.sqlite3", "db.sqlite3")
    buffer.seek(0)
    return buffer


def make_backup_zip():
    """Create a consistent SQLite + media archive with SHA-256 manifest."""
    if connection.vendor != "sqlite":
        raise RuntimeError("Автоматична резервна копія підтримується лише для SQLite")
    with tempfile.TemporaryDirectory(prefix="autozapaz-backup-") as temp_dir:
        snap = Path(temp_dir) / "db.sqlite3"
        try:
            with connection.cursor() as cur:
                cur.execute("VACUUM INTO %s", [str(snap)])
        except Exception as exc:
            raise RuntimeError(f"Не вдалося створити копію бази даних: {exc}") from exc

        integrity = sqlite3.connect(str(snap))
        try:
            check = integrity.execute("PRAGMA integrity_check").fetchone()
        finally:
            integrity.close()
        if not check or check[0] != "ok":
            raise RuntimeError(f"Перевірка SQLite backup не пройдена: {check[0] if check else 'немає відповіді'}")

        files = {"db.sqlite3": {"sha256": _sha256_file(snap), "size": snap.stat().st_size}}
        media = Path(settings.MEDIA_ROOT)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snap, "db.sqlite3")
            if media.exists():
                media_root = media.resolve()
                for file_path in media.rglob("*"):
                    if file_path.is_symlink() or not file_path.is_file():
                        continue
                    resolved = file_path.resolve()
                    if media_root not in resolved.parents:
                        continue
                    archive_name = "media/" + file_path.relative_to(media).as_posix()
                    archive.write(file_path, archive_name)
                    files[archive_name] = {"sha256": _sha256_file(file_path), "size": file_path.stat().st_size}
            manifest = {
                "format_version": 1,
                "created_at": timezone.localtime().isoformat(),
                "database_engine": "sqlite",
                "files": files,
            }
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr(
                "README.txt",
                "Резервна копія АвтоЗапас. Перед відновленням зупиніть застосунок. "
                "Спочатку перевірте архів командою: python manage.py verify_backup <архів.zip>.\n",
            )
        data = buf.getvalue()
        try:
            verify_backup_archive(data)
        except ValueError as exc:
            raise RuntimeError(f"Створена резервна копія не пройшла перевірку: {exc}") from exc
        return data


def save_backup(keep=14):
    if keep < 1:
        raise ValueError("Кількість резервних копій для зберігання має бути не меншою за 1.")
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        BACKUP_DIR.chmod(0o700)
    stamp = timezone.localtime()
    path = BACKUP_DIR / f"backup-{stamp:%Y%m%d-%H%M%S}.zip"
    while path.exists():
        stamp += timedelta(seconds=1)
        path = BACKUP_DIR / f"backup-{stamp:%Y%m%d-%H%M%S}.zip"

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".autozapaz-backup-", suffix=".tmp", dir=BACKUP_DIR, delete=False) as output:
            temporary_path = Path(output.name)
            output.write(make_backup_zip())
            output.flush()
            os.fsync(output.fileno())
        verify_backup_archive(temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)

    old = sorted(p for p in BACKUP_DIR.glob("backup-*.zip") if BACKUP_NAME.match(p.name))
    for backup_path in old[:-keep]:
        backup_path.unlink(missing_ok=True)
    return path


@local_admin_only
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


@local_admin_only
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


def category_by_path(path, company):
    parent = None
    for part in [p.strip() for p in re.split(r"→|>|/|\\", path) if p.strip()]:
        parent, _ = Category.objects.get_or_create(company=company, name=part[:100], parent=parent)
    return parent


@local_admin_only
def product_export(request):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="products.csv"'
    resp.write("\ufeff")
    w = csv.writer(resp, delimiter=";")
    w.writerow([label for _, label in COLUMNS])
    if request.GET.get("template"):
        w.writerow(["COLA05", "Кола 0.5л", "4820000000011", "Напої → Газовані напої", "пляшка", "20.00", "30.00", "10", "100", "1", "1", "0", "SACHS"])
        return resp
    for p in Product.objects.filter(company=request.company).select_related("category").order_by("name"):
        w.writerow([p.sku, p.name, p.barcode or "", str(p.category or ""), p.unit, f"{p.cost_price:.2f}", f"{p.price:.2f}",
                    f"{p.min_stock.normalize():f}", f"{p.current_stock.normalize():f}", int(p.is_active), int(p.is_favorite),
                    int(p.allow_price_change), p.brand])
    return resp


def parse_csv(raw, company):
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
    for p in Product.objects.filter(company=company):
        existing[(p.brand.upper(), p.sku_norm)] = p
        by_norm.setdefault(p.sku_norm, []).append(p)
    bc_owner = {p.barcode: ((p.brand.upper(), p.sku_norm), p.sku) for p in Product.objects.filter(company=company).exclude(barcode__isnull=True)}
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
def apply_rows(rows, user, company):
    created = updated = 0
    for r in rows:
        p = Product.objects.filter(company=company, pk=r["pk"]).first() if r.get("pk") else None
        if r.get("pk") and p is None:
            raise ValueError("Товар з імпорту не належить активному магазину")
        is_new = p is None
        if is_new:
            p = Product(company=company, sku=r["sku"], name=r["name"])
        for f in ("name", "unit", "barcode", "brand"):
            if f in r:
                setattr(p, f, r[f])
        for f in ("is_active", "is_favorite", "allow_price_change"):
            if f in r:
                setattr(p, f, r[f])
        if "min_stock" in r:
            p.min_stock = Decimal(r["min_stock"])
        if "category" in r:
            p.category = category_by_path(r["category"], company)
        for f in ("price", "cost_price"):
            if f in r:
                new, old = Decimal(r[f]), getattr(p, f)
                setattr(p, f, new)
                if not is_new and old != new:
                    PriceHistory.objects.create(company=company, product=p, field=f, old_value=old, new_value=new, changed_by=user)
        # Якщо в CSV є закупівельна ціна, але немає продажної, застосовуємо
        # ті самі правила націнки, що й у картці товару. Явно задану ціну
        # продажу ніколи не перезаписуємо.
        p.save()
        if "cost_price" in r and "price" not in r and p.cost_price > 0:
            pricing.reprice_product(p, user)
        if is_new:
            if Decimal(r.get("current_stock", "0")) > 0:
                services.add_movement(p, Decimal(r["current_stock"]), "opening", "", user, "Імпорт CSV")
            created += 1
        else:
            updated += 1
    return created, updated


@local_admin_only
def product_import(request):
    if request.method == "POST":
        f = request.FILES.get("file")
        if not f or f.size > 5_000_000:
            messages.error(request, "Оберіть CSV-файл (до 5 МБ)")
            return redirect("product_import")
        rows, errors, fatal = parse_csv(f.read(), request.company)
        if fatal:
            messages.error(request, fatal)
            return redirect("product_import")
        request.session["product_import"] = rows
        request.session["product_import_company_id"] = request.company.pk
        return render(request, "core/product_import_preview.html", {
            "rows": rows[:200], "total": len(rows), "new": sum(1 for r in rows if r["new"]),
            "upd": sum(1 for r in rows if not r["new"]), "errors": errors[:200], "n_err": len(errors)})
    return render(request, "core/product_import.html", {"columns": COLUMNS})


@local_admin_only
@require_POST
def product_import_apply(request):
    rows = request.session.pop("product_import", None)
    import_company_id = request.session.pop("product_import_company_id", None)
    if import_company_id != request.company.pk:
        messages.error(request, "Активний магазин змінився. Завантажте CSV-файл ще раз.")
        return redirect("product_import")
    if not rows:
        messages.error(request, "Немає даних для імпорту — завантажте файл заново")
        return redirect("product_import")
    try:
        c, u = apply_rows(rows, request.user, request.company)
    except Exception as e:  # whole import is atomic: nothing is saved on failure
        messages.error(request, f"Імпорт скасовано, жодних змін не збережено: {e}")
        return redirect("product_import")
    messages.success(request, f"Імпорт завершено: створено {c}, оновлено {u}")
    return redirect("product_list")
