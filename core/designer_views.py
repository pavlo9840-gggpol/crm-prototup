"""Views for the print-form designer and for printing documents."""
import io
import json
import zipfile
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from . import designer, docs
from .builtin_templates import BUILTINS
from . import legacy
from .models import DocTemplate, PrintTemplate, StockAdjustment
from .views import staff_only


# ---------------------------------------------------------------- helpers
def ensure_builtin():
    """Create / refresh standard templates (they are replaced on update; custom ones never are)."""
    for key, dt, name, factory in BUILTINS:
        layout, _ = designer.sanitize(factory(), dt)
        obj, created = PrintTemplate.objects.get_or_create(
            builtin_key=key, defaults={"doc_type": dt, "name": name, "layout": layout, "is_builtin": True,
                                       "format_version": designer.FORMAT_VERSION})
        if not created and (obj.layout != layout or obj.name != name or not obj.is_builtin):
            obj.layout, obj.name, obj.doc_type, obj.is_builtin = layout, name, dt, True
            obj.format_version = designer.FORMAT_VERSION
            obj.save(update_fields=["layout", "name", "doc_type", "is_builtin", "format_version", "updated_at"])


def templates_for(doc_type):
    ensure_builtin()
    return list(PrintTemplate.objects.filter(doc_type=doc_type))


def default_template(doc_type):
    ensure_builtin()
    t = PrintTemplate.objects.filter(doc_type=doc_type, is_default=True).first()
    return t or PrintTemplate.objects.filter(doc_type=doc_type, is_builtin=True).order_by("id").first()


def pick_template(doc_type, raw_id):
    if str(raw_id or "").isdigit():
        t = PrintTemplate.objects.filter(pk=raw_id, doc_type=doc_type).first()
        if t:
            return t
    return default_template(doc_type)


def print_bar(doc_type):
    """Context for the group-print bar included in list pages."""
    return {"ptype": doc_type, "ptpls": templates_for(doc_type)}


def _read_json(request):
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _sanitize_or_400(layout, doc_type):
    try:
        return designer.sanitize(layout, doc_type), None
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return None, JsonResponse({"error": f"Некоректний шаблон: {e}"}, status=400)


def output(request, pages, fmt, name, extra=None):
    """Build the HTTP response for resolved pages in html / pdf / png / jpg."""
    if fmt in ("pdf", "png", "jpg"):
        pdf = designer.render_pdf(pages)
        if fmt == "pdf":
            r = HttpResponse(pdf, content_type="application/pdf")
            r["Content-Disposition"] = f'inline; filename="{name}.pdf"'
            return r
        if not designer.png_available():
            return HttpResponse("Експорт у зображення недоступний: встановіть pymupdf (pip install pymupdf).",
                                status=501, content_type="text/plain; charset=utf-8")
        imgs = designer.render_images(pdf, fmt)
        mime = "image/png" if fmt == "png" else "image/jpeg"
        if len(imgs) == 1:
            r = HttpResponse(imgs[0], content_type=mime)
            r["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
            return r
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i, b in enumerate(imgs, 1):
                z.writestr(f"{name}-{i}.{fmt}", b)
        r = HttpResponse(buf.getvalue(), content_type="application/zip")
        r["Content-Disposition"] = f'attachment; filename="{name}.zip"'
        return r
    ctx = {"pages_html": mark_safe("".join(designer.render_page_html(p) for p in pages)), "css": designer.PAGE_CSS,
           "pw": pages[0]["w"], "ph": pages[0]["h"], "npages": len(pages), "png_ok": designer.png_available()}
    ctx.update(extra or {})
    return render(request, "core/designer_print.html", ctx)


# ---------------------------------------------------------------- printing
@login_required
def print_docs(request, doc_type):
    if doc_type not in docs.DOC_TYPES:
        raise Http404
    if doc_type != "receipt" and not request.user.is_staff:
        raise PermissionDenied
    ctxs = docs.load(doc_type, request)
    tpl = pick_template(doc_type, request.GET.get("template"))
    res, err = _sanitize_or_400(tpl.layout, doc_type)
    if err:
        return err
    clean = res[0]
    if request.GET.get("fmt") == "txt" and doc_type == "receipt":
        try:
            cols = max(24, min(64, int(request.GET.get("cols", 42))))
        except ValueError:
            cols = 42
        sep = "\n\n" + "=" * cols + "\n\n"
        r = HttpResponse(sep.join(docs.receipt_text(c, cols) for c in ctxs), content_type="text/plain; charset=utf-8")
        r["Content-Disposition"] = 'inline; filename="receipt.txt"'
        return r
    pages = [pg for c in ctxs for pg in designer.resolve(clean, c)]
    keep = request.GET.copy()
    for k in ("template", "fmt", "auto"):
        keep.pop(k, None)
    extra = {"doc_type": doc_type, "doc_label": docs.DOC_TYPES[doc_type]["label"], "tpl": tpl,
             "templates": templates_for(doc_type), "qs": keep.urlencode(), "auto": request.GET.get("auto") == "1"}
    return output(request, pages, request.GET.get("fmt", "html"), doc_type, extra)


@login_required
def sale_print(request, pk):
    """Backward-compatible URL used by older pages."""
    kind = request.GET.get("kind", "receipt")
    kind = kind if kind in ("receipt", "sale_invoice") else "receipt"
    q = {"ids": pk}
    for k in ("auto", "fmt", "template"):
        if request.GET.get(k):
            q[k] = request.GET[k]
    return redirect(f"/print/{kind}/?{urlencode(q)}")


@staff_only
def purchase_print(request, pk):
    return redirect(f"/print/purchase/?ids={pk}")


@staff_only
def adjustment_list(request):
    qs = StockAdjustment.objects.select_related("product", "user").order_by("-created_at")[:300]
    return render(request, "core/adjustment_list.html", {"adjustments": qs, **print_bar("writeoff")})


# ---------------------------------------------------------------- designer pages
@staff_only
def designer_home(request):
    ensure_builtin()
    cards = []
    for key, spec in docs.DOC_TYPES.items():
        tpls = list(PrintTemplate.objects.filter(doc_type=key))
        d = default_template(key)
        cards.append({"key": key, "label": spec["label"], "count": len(tpls), "custom": sum(1 for t in tpls if not t.is_builtin),
                      "default": d.name if d else ""})
    old = [{"obj": o, "label": legacy.LEGACY_KINDS.get(o.kind, o.kind), "ok": o.kind in legacy.LEGACY_KINDS}
           for o in DocTemplate.objects.all().order_by("kind")]
    return render(request, "core/designer_home.html", {"cards": cards, "legacy": old})


@staff_only
def designer_select(request):
    doc_type = request.GET.get("type", "")
    if doc_type not in docs.DOC_TYPES:
        return redirect("designer_home")
    d = default_template(doc_type)
    return render(request, "core/designer_select.html", {
        "dt": doc_type, "label": docs.DOC_TYPES[doc_type]["label"], "templates": templates_for(doc_type),
        "default_id": d.pk if d else None})


@staff_only
def designer_edit(request, pk):
    t = get_object_or_404(PrintTemplate, pk=pk)
    res, err = _sanitize_or_400(t.layout, t.doc_type)
    if err:
        raise Http404("Шаблон пошкоджено")
    clean, warnings = res
    spec = docs.DOC_TYPES[t.doc_type]
    sample = docs.sample_ctx(t.doc_type)
    keys = docs.all_field_keys(t.doc_type)
    init = {
        "id": t.pk, "name": t.name, "doc_type": t.doc_type, "builtin": t.is_builtin, "layout": clean,
        "fields": [{"group": g, "fields": [[k, l] for k, l in fl]} for g, fl in docs.fields_for(t.doc_type)],
        "cols": [[k, l] for k, l in spec["cols"]], "sample": {k: designer.get_path(sample, k) for k in keys},
        "sample_items": sample["items"][:4], "has_logo": bool(sample["_images"].get("company_logo")),
        "version": designer.FORMAT_VERSION, "png_ok": designer.png_available(), "warnings": warnings,
        "urls": {"save": f"/designer/save/{t.pk}/", "save_as": f"/designer/save-as/{t.pk}/",
                 "preview": f"/designer/preview/{t.pk}/", "back": f"/designer/select/?type={t.doc_type}"},
    }
    return render(request, "core/designer_edit.html", {"t": t, "init": init, "label": spec["label"]})


@staff_only
@require_POST
def designer_save(request, pk):
    t = get_object_or_404(PrintTemplate, pk=pk)
    if t.is_builtin:
        return JsonResponse({"error": "Стандартні шаблони не редагуються — скористайтеся «Зберегти як новий»"}, status=403)
    data = _read_json(request)
    name = str(data.get("name", "")).strip()[:150]
    if not name:
        return JsonResponse({"error": "Вкажіть назву шаблону"}, status=400)
    res, err = _sanitize_or_400(data.get("layout"), t.doc_type)
    if err:
        return err
    t.name, t.layout, t.format_version = name, res[0], designer.FORMAT_VERSION
    t.save()
    return JsonResponse({"ok": True, "warnings": res[1], "layout": res[0]})


@staff_only
@require_POST
def designer_save_as(request, pk):
    src = get_object_or_404(PrintTemplate, pk=pk)
    data = _read_json(request)
    name = str(data.get("name", "")).strip()[:150]
    if not name:
        return JsonResponse({"error": "Вкажіть назву шаблону"}, status=400)
    res, err = _sanitize_or_400(data.get("layout"), src.doc_type)
    if err:
        return err
    n = PrintTemplate.objects.create(doc_type=src.doc_type, name=name, layout=res[0], format_version=designer.FORMAT_VERSION)
    return JsonResponse({"ok": True, "url": f"/designer/edit/{n.pk}/", "warnings": res[1]})


@staff_only
@require_POST
def designer_new(request, doc_type):
    if doc_type not in docs.DOC_TYPES:
        raise Http404
    base = default_template(doc_type)
    n = PrintTemplate.objects.create(doc_type=doc_type, name=f"Новий шаблон ({docs.DOC_TYPES[doc_type]['label']})",
                                     layout=base.layout, format_version=designer.FORMAT_VERSION)
    return redirect("designer_edit", pk=n.pk)


@staff_only
@require_POST
def designer_copy(request, pk):
    src = get_object_or_404(PrintTemplate, pk=pk)
    n = PrintTemplate.objects.create(doc_type=src.doc_type, name=f"{src.name} (копія)"[:150], layout=src.layout,
                                     format_version=src.format_version)
    messages.success(request, "Створено копію — тепер її можна редагувати")
    return redirect("designer_edit", pk=n.pk)


@staff_only
@require_POST
def designer_delete(request, pk):
    t = get_object_or_404(PrintTemplate, pk=pk)
    if t.is_builtin:
        raise PermissionDenied
    dt = t.doc_type
    t.delete()
    messages.success(request, "Шаблон видалено")
    return redirect(f"/designer/select/?type={dt}")


@staff_only
@require_POST
def designer_default(request, pk):
    t = get_object_or_404(PrintTemplate, pk=pk)
    PrintTemplate.objects.filter(doc_type=t.doc_type).update(is_default=False)
    t.is_default = True
    t.save(update_fields=["is_default"])
    messages.success(request, f"«{t.name}» тепер шаблон за замовчуванням")
    return redirect(f"/designer/select/?type={t.doc_type}")


def envelope(t):
    return {"app": "retail_shop", "kind": "print_template", "format_version": designer.FORMAT_VERSION,
            "doc_type": t.doc_type, "name": t.name, "layout": t.layout}


@staff_only
def designer_export(request, pk):
    t = get_object_or_404(PrintTemplate, pk=pk)
    r = HttpResponse(json.dumps(envelope(t), ensure_ascii=False, indent=1), content_type="application/json; charset=utf-8")
    r["Content-Disposition"] = f'attachment; filename="template-{t.doc_type}-{t.pk}.json"'
    return r


def _import_one(data, make_default):
    """Create a NEW custom template from an exported envelope. Raises ValueError on invalid input."""
    if not isinstance(data, dict) or data.get("kind") != "print_template" or data.get("doc_type") not in docs.DOC_TYPES:
        raise ValueError("це не файл шаблону цієї системи (підтримується лише власний формат JSON)")
    layout = dict(data.get("layout") or {})
    layout.setdefault("format_version", data.get("format_version", 1))
    try:
        clean, warns = designer.sanitize(layout, data["doc_type"])
    except (TypeError, KeyError, AttributeError) as e:
        raise ValueError(f"пошкоджена структура ({e})")
    name = str(data.get("name") or "Імпортований шаблон").strip()[:130] + " (імпорт)"
    t = PrintTemplate.objects.create(doc_type=data["doc_type"], name=name, layout=clean, format_version=designer.FORMAT_VERSION)
    if make_default:
        PrintTemplate.objects.filter(doc_type=t.doc_type).update(is_default=False)
        t.is_default = True
        t.save(update_fields=["is_default"])
    return t, warns


@staff_only
@require_POST
def designer_import(request):
    f = request.FILES.get("file")
    if not f or f.size > 20_000_000:
        messages.error(request, "Оберіть файл шаблону (JSON або ZIP)")
        return redirect("designer_home")
    make_default = bool(request.POST.get("make_default"))
    blobs = []
    try:
        raw = f.read()
        if f.name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos = [i for i in z.infolist() if i.filename.lower().endswith(".json") and i.file_size < 5_000_000][:200]
                blobs = [(i.filename, z.read(i)) for i in infos]
        else:
            blobs = [(f.name, raw)]
    except zipfile.BadZipFile:
        messages.error(request, "Не вдалося прочитати ZIP-архів")
        return redirect("designer_home")
    done, last = [], None
    for fname, blob in blobs:
        try:
            t, warns = _import_one(json.loads(blob.decode("utf-8-sig")), make_default and len(blobs) == 1)
        except (ValueError, UnicodeDecodeError) as e:
            messages.error(request, f"Не вдалося завантажити «{fname}»: {e}")
            continue
        done.append(t)
        last = t
        for w in warns[:4]:
            messages.warning(request, f"⚠ «{t.name}»: {w}")
        if warns:
            messages.warning(request, f"«{t.name}» може бути несумісним — перевірте його в дизайнері та збережіть виправлену копію.")
    if not done:
        return redirect("designer_home")
    messages.success(request, f"Завантажено шаблонів: {len(done)}. Вони додані як нові — існуючі шаблони не змінено.")
    return redirect(f"/designer/select/?type={last.doc_type}")


@staff_only
def designer_export_all(request):
    """Backup of all custom templates (one JSON per template inside a ZIP)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in PrintTemplate.objects.filter(is_builtin=False):
            z.writestr(f"{t.doc_type}-{t.pk}.json", json.dumps(envelope(t), ensure_ascii=False, indent=1))
    r = HttpResponse(buf.getvalue(), content_type="application/zip")
    r["Content-Disposition"] = 'attachment; filename="print-templates-backup.zip"'
    return r


@staff_only
def designer_preview(request, pk):
    t = get_object_or_404(PrintTemplate, pk=pk)
    layout = t.layout
    if request.method == "POST":
        layout = _read_json(request).get("layout")
    res, err = _sanitize_or_400(layout, t.doc_type)
    if err:
        return err
    pages = designer.resolve(res[0], docs.sample_ctx(t.doc_type))
    return output(request, pages, request.GET.get("fmt", "html"), f"preview-{t.doc_type}",
                  {"preview": True, "doc_type": t.doc_type, "tpl": t})


def font_view(request, style):
    name = {"regular": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf"}.get(style)
    if not name:
        raise Http404
    r = FileResponse(open(designer.FONT_DIR / name, "rb"), content_type="font/ttf")
    r["Cache-Control"] = "public, max-age=86400"
    return r


# ---------------------------------------------------------------- legacy (v2 HTML) templates
@staff_only
@require_POST
def legacy_convert(request, pk):
    o = get_object_or_404(DocTemplate, pk=pk)
    if o.kind not in legacy.LEGACY_KINDS:
        messages.error(request, "Цей тип старого шаблону не підтримується")
        return redirect("designer_home")
    try:
        layout, warns = designer.sanitize(legacy.convert(o.body, o.paper, o.kind), o.kind)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        messages.error(request, f"Не вдалося перетворити шаблон: {e}")
        return redirect("designer_home")
    t = PrintTemplate.objects.create(doc_type=o.kind, name=f"{legacy.LEGACY_KINDS[o.kind]} (зі старої версії)", layout=layout,
                                     format_version=designer.FORMAT_VERSION)
    messages.success(request, "Старий шаблон перетворено на новий. Перевірте вигляд і за потреби підкоригуйте — "
                              "перетворення автоматичне й наближене.")
    return redirect("designer_edit", pk=t.pk)


@staff_only
@require_POST
def legacy_delete(request, pk=None):
    qs = DocTemplate.objects.all() if pk is None else DocTemplate.objects.filter(pk=pk)
    n = qs.count()
    qs.delete()
    messages.success(request, f"Видалено старих шаблонів: {n}")
    return redirect("designer_home")
