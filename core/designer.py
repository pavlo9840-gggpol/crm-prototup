"""Print-form engine.

Layout JSON (format_version 1):
  {"format_version": 1,
   "page": {"preset": "a4", "w": 210, "h": 297, "auto_height": false},
   "elements": [ {id,type,x,y,w,h,group,flow,show_if,bold_if, ...type specific} ]}
Types: text, field, table, image, line, rect, barcode, qr.  All sizes in millimetres, fonts in pt.

Pipeline:  layout + ctx --resolve()--> page{w,h,prims}  --render_html()/render_pdf()--> output
Primitives: text, rect, line, img, fill (black rectangles: barcode bars and QR modules).
"""
import base64
import html
import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from reportlab.graphics.barcode import code128, qrencoder
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from . import docs

FORMAT_VERSION = 1
MIN_FORMAT_VERSION = 1
FONT_DIR = Path(__file__).resolve().parent / "fonts"
PT = 25.4 / 72.0          # mm per pt
LH = 1.2                  # line height factor
ELEMENT_TYPES = ("text", "field", "table", "image", "line", "rect", "barcode", "qr")
OPS = ("filled", "empty", "eq", "ne", "gt", "ge", "lt", "le", "contains")
PRESETS = {
    "a4": (210, 297, False), "a4l": (297, 210, False), "a5": (148, 210, False), "a5l": (210, 148, False),
    "roll80": (80, 150, True), "roll58": (58, 120, True), "tag58": (58, 40, False), "custom": (100, 100, False),
}
DATA_URI = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+$")
HEX = re.compile(r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")
MAX_IMAGE = 900_000

TOKEN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_fonts = False


def ensure_fonts():
    global _fonts
    if not _fonts:
        pdfmetrics.registerFont(TTFont("DV", str(FONT_DIR / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DVB", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
        _fonts = True


def font_name(bold):
    return "DVB" if bold else "DV"


def text_w(s, size, bold=False):
    ensure_fonts()
    return pdfmetrics.stringWidth(s, font_name(bold), size) * PT


# ---------------------------------------------------------------- sanitising
def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return round(max(lo, min(hi, v)), 2)


def _str(v, n):
    return str(v if v is not None else "")[:n]


def _choice(v, allowed, default):
    try:
        return v if v in allowed else default
    except TypeError:
        return default


def _cond(c):
    if not isinstance(c, dict) or not c.get("field"):
        return None
    return {"field": _str(c["field"], 60), "op": _choice(c.get("op"), OPS, "filled"), "value": _str(c.get("value", ""), 60)}


def sanitize(layout, doc_type):
    """Return (clean_layout, warnings). Raises ValueError for unusable input."""
    if not isinstance(layout, dict) or not isinstance(layout.get("elements", []), list):
        raise ValueError("Некоректна структура шаблону")
    warnings = []
    ver = layout.get("format_version", 1)
    if not isinstance(ver, int) or ver > FORMAT_VERSION or ver < MIN_FORMAT_VERSION:
        warnings.append(f"Шаблон створено для іншої версії формату ({ver}); поточна — {FORMAT_VERSION}. "
                        "Він може відображатися некоректно.")
    pg = layout.get("page") if isinstance(layout.get("page"), dict) else {}
    preset = _choice(pg.get("preset"), PRESETS, "custom")
    dw, dh, da = PRESETS[preset]
    page = {"preset": preset, "w": _num(pg.get("w"), 20, 1000, dw), "h": _num(pg.get("h"), 10, 3000, dh),
            "auto_height": bool(pg.get("auto_height", da))}
    keys = docs.all_field_keys(doc_type)
    col_keys = {k for k, _ in docs.DOC_TYPES[doc_type]["cols"]}
    seen, out = set(), []
    for i, e in enumerate(layout.get("elements", [])[:400]):
        if not isinstance(e, dict) or e.get("type") not in ELEMENT_TYPES:
            warnings.append(f"Елемент №{i + 1} пропущено (невідомий тип)")
            continue
        t = e["type"]
        eid = _str(e.get("id"), 20) or f"e{i + 1}"
        while eid in seen:
            eid += "x"
        seen.add(eid)
        c = {"id": eid, "type": t, "x": _num(e.get("x"), -50, 3000, 0), "y": _num(e.get("y"), -50, 5000, 0),
             "w": _num(e.get("w"), 0.2, 3000, 20), "h": _num(e.get("h"), 0.2, 5000, 6), "group": _str(e.get("group"), 20),
             "flow": bool(e.get("flow", False)), "show_if": _cond(e.get("show_if"))}
        if t in ("text", "field", "table"):
            c["size"] = _num(e.get("size"), 4, 72, 9)
        if t in ("text", "field"):
            c.update(bold=bool(e.get("bold")), align=_choice(e.get("align"), ("left", "center", "right"), "left"),
                     valign=_choice(e.get("valign"), ("top", "middle", "bottom"), "top"), bold_if=_cond(e.get("bold_if")))
        if t == "text":
            c["text"] = _str(e.get("text"), 3000)
            for tok in TOKEN.findall(c["text"]):
                if tok not in keys:
                    warnings.append(f"Підстановка «{{{{{tok}}}}}» не належить цьому типу документа — буде порожньою")
        elif t == "field":
            c.update(field=_str(e.get("field"), 60), prefix=_str(e.get("prefix"), 100), suffix=_str(e.get("suffix"), 100),
                     format=_choice(e.get("format"), ("text", "money", "upper"), "text"),
                     empty=_choice(e.get("empty"), ("show", "hide", "dash"), "show"))
            if c["field"] and c["field"] not in keys:
                warnings.append(f"Поле «{c['field']}» не належить цьому типу документа — буде порожнім")
        elif t == "table":
            cols = []
            for col in (e.get("cols") if isinstance(e.get("cols"), list) else [])[:12]:
                if not isinstance(col, dict):
                    continue
                k = re.sub(r"[^a-z_]", "", _str(col.get("key"), 20))
                if k not in col_keys:
                    warnings.append(f"Колонка «{k}» не підтримується для цього документа — буде порожньою")
                cols.append({"key": k, "title": _str(col.get("title"), 40), "w": _num(col.get("w"), 3, 400, 20),
                             "align": _choice(col.get("align"), ("left", "center", "right"), "left"),
                             "wrap": bool(col.get("wrap"))})
            c.update(cols=cols or [{"key": "name", "title": "Назва", "w": 60, "align": "left", "wrap": True}],
                     row_h=_num(e.get("row_h"), 3, 60, 6), head_h=_num(e.get("head_h"), 3, 60, 6),
                     head=bool(e.get("head", True)), border=bool(e.get("border", True)))
        elif t == "image":
            src = _str(e.get("src"), MAX_IMAGE + 100)
            if src != "company_logo" and not (DATA_URI.match(src) and len(src) <= MAX_IMAGE):
                if src:
                    warnings.append("Зображення відхилено (непідтримуваний формат або завеликий розмір)")
                src = ""
            c["src"] = src
        elif t == "line":
            c.update(dir=_choice(e.get("dir"), ("h", "v"), "h"), thick=_num(e.get("thick"), 0.05, 5, 0.3))
        elif t == "rect":
            fill = _str(e.get("fill"), 9)
            c.update(border=_num(e.get("border"), 0, 5, 0.3), fill=fill if HEX.match(fill) else "")
        elif t in ("barcode", "qr"):
            c.update(field=_str(e.get("field"), 60), static=_str(e.get("static"), 200))
            if t == "barcode":
                c.update(show_text=bool(e.get("show_text", True)), size=_num(e.get("size"), 4, 20, 7))
            if c["field"] and c["field"] not in keys:
                warnings.append(f"Поле «{c['field']}» не належить цьому типу документа")
        for k in ("show_if", "bold_if"):
            if k in c and c[k] and c[k]["field"] not in keys:
                warnings.append(f"Умова «{c[k]['field']}» не належить цьому типу документа")
        out.append(c)
    return {"format_version": FORMAT_VERSION, "page": page, "elements": out}, warnings


# ---------------------------------------------------------------- data access
def get_path(ctx, key):
    cur = ctx
    for part in (key or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return ""
        cur = cur[part]
    return "" if cur is None else str(cur)


def _dec(s):
    try:
        return Decimal(str(s).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def check(cond, ctx):
    """True/False for a condition dict; None condition -> None."""
    if not cond:
        return None
    v, ref, op = get_path(ctx, cond["field"]), cond.get("value", ""), cond["op"]
    if op == "filled":
        return v.strip() != ""
    if op == "empty":
        return v.strip() == ""
    if op == "contains":
        return ref.lower() in v.lower()
    a, b = _dec(v), _dec(ref)
    if a is not None and b is not None:
        return {"eq": a == b, "ne": a != b, "gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
    return {"eq": v == ref, "ne": v != ref}.get(op, False)


def fmt_value(v, how):
    if how == "upper":
        return v.upper()
    if how == "money" and v.strip():
        d = _dec(v)
        if d is not None:
            return f"{d:.2f}"
    return v


# ---------------------------------------------------------------- text layout
def wrap(text, size, bold, width_mm):
    """Greedy word wrap with hard breaks for over-long words. Returns list of lines."""
    ensure_fonts()
    fn, width = font_name(bold), max(width_mm, 1) / PT

    def sw(s):
        return pdfmetrics.stringWidth(s, fn, size)
    lines = []
    for para in str(text).replace("\r", "").split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = word if not cur else cur + " " + word
            if sw(cand) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while sw(word) > width and len(word) > 1:      # break long word
                n = len(word)
                while n > 1 and sw(word[:n]) > width:
                    n -= 1
                lines.append(word[:n])
                word = word[n:]
            cur = word
        lines.append(cur)
    return lines


def ellipsize(text, size, bold, width_mm):
    ensure_fonts()
    fn, width = font_name(bold), max(width_mm, 1) / PT
    if pdfmetrics.stringWidth(text, fn, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "…", fn, size) > width:
        text = text[:-1]
    return text + "…"


def lh_mm(size):
    return size * PT * LH


# ---------------------------------------------------------------- barcode / qr
def barcode_rects(value, x, y, w, h):
    """Code128 as black rectangles inside box; None if value is not encodable."""
    if not value or not all(32 <= ord(c) < 127 for c in value):
        return None
    b = code128.Code128(value, barWidth=1, humanReadable=False)
    b.validate(); b.encode(); b.decompose()
    pos, bars = 0, []
    for ch in b.decomposed:
        width = ord(ch.lower()) - 96
        if ch.isupper():
            bars.append((pos, width))
        pos += width
    quiet = min(2.5, w * 0.04)
    k = (w - 2 * quiet) / pos
    return [(x + quiet + s * k, y, bw * k, h) for s, bw in bars]


def qr_rects(value, x, y, w, h):
    if not value:
        return None
    q = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    q.addData(value); q.make()
    n = q.getModuleCount()
    side = min(w, h)
    m = side / (n + 2)
    ox, oy = x + (w - side) / 2 + m, y + (h - side) / 2 + m
    rects = []
    for r in range(n):
        c = 0
        while c < n:
            if q.isDark(r, c):
                s = c
                while c < n and q.isDark(r, c):
                    c += 1
                rects.append((ox + s * m, oy + r * m, (c - s) * m, m))
            else:
                c += 1
    return rects


# ---------------------------------------------------------------- resolve
def _img_bytes(src, ctx):
    if src == "company_logo":
        return ctx.get("_images", {}).get("company_logo")
    m = DATA_URI.match(src or "")
    return base64.b64decode(src.split(",", 1)[1]) if m else None


def _text_value(e, ctx):
    """-> (text, hidden_empty)"""
    if e["type"] == "text":
        return TOKEN.sub(lambda m: get_path(ctx, m.group(1)), e.get("text", "")), False
    v = fmt_value(get_path(ctx, e.get("field", "")), e.get("format", "text"))
    if v.strip() == "":
        if e.get("empty") == "hide":
            return "", True
        if e.get("empty") == "dash":
            v = "—"
    return e.get("prefix", "") + v + e.get("suffix", ""), False


def _table_layout(e, ctx):
    """-> (prims relative to (0,0), actual_height, head_height, [row heights])"""
    cols, size = e["cols"], e["size"]
    k = e["w"] / sum(c["w"] for c in cols)
    xs, x = [], 0.0
    for c in cols:
        xs.append((x, c["w"] * k))
        x += c["w"] * k
    prims, y, lh, pad = [], 0.0, lh_mm(size), 1.0
    row_hs, head_used = [], 0.0
    bounds = []                              # y of horizontal separators

    def cell(i, text, y0, rh, bold, wrap_it):
        cx, cw = xs[i]
        lines = wrap(text, size, bold, cw - 2 * pad) if wrap_it else [ellipsize(text, size, bold, cw - 2 * pad)]
        return lines, cx, cw

    if e["head"]:
        for i, c in enumerate(cols):
            lines, cx, cw = cell(i, c["title"], 0, e["head_h"], True, True)
            lines = lines[:max(1, int(e["head_h"] / lh + 0.05))]
            prims.append({"t": "text", "x": cx + pad, "y": (e["head_h"] - len(lines) * lh) / 2, "w": cw - 2 * pad,
                          "lines": lines, "size": size, "bold": True, "align": c["align"], "lh": lh})
        prims.insert(0, {"t": "rect", "x": 0, "y": 0, "w": e["w"], "h": e["head_h"], "border": 0, "fill": "#e9e9e9"})
        y = head_used = e["head_h"]
        bounds.append(y)
    for row in ctx.get("items", []):
        cells, rh = [], e["row_h"]
        for i, c in enumerate(cols):
            lines, cx, cw = cell(i, str(row.get(c["key"], "")), y, rh, False, c["wrap"])
            cells.append((lines, cx, cw, c["align"]))
            rh = max(rh, len(lines) * lh + 1.4)
        for lines, cx, cw, al in cells:
            prims.append({"t": "text", "x": cx + pad, "y": y + (rh - len(lines) * lh) / 2, "w": cw - 2 * pad,
                          "lines": lines, "size": size, "bold": False, "align": al, "lh": lh})
        y += rh
        row_hs.append(rh)
        bounds.append(y)
    total = y
    if e["border"] and total > 0:
        t = 0.2
        prims.append({"t": "rect", "x": 0, "y": 0, "w": e["w"], "h": total, "border": t, "fill": ""})
        for b in bounds[:-1]:
            prims.append({"t": "line", "x1": 0, "y1": b, "x2": e["w"], "y2": b, "w": t})
        for cx, _ in xs[1:]:
            prims.append({"t": "line", "x1": cx, "y1": 0, "x2": cx, "y2": total, "w": t})
    return prims, total, head_used, row_hs


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0) > 0.5


def _resolve_one(layout, ctx, plan=None):
    """Apply data + flow layout to ONE page. Returns {"w","h","prims","meta"} (coordinates in mm).
    plan (pagination): {"main": element id, "rows": [...], "drop_above": bool, "drop_below": bool, "top": mm}"""
    ensure_fonts()
    page, els = layout["page"], layout["elements"]
    EPS = 0.05
    if plan:
        T = next(e for e in els if e["id"] == plan["main"])
        keep = []
        for e in els:
            if e is T:
                keep.append(e)
                continue
            below = e["y"] >= T["y"] + T["h"] - EPS
            if plan["drop_above"] and not below:
                continue
            if plan["drop_below"] and below:
                continue
            keep.append(e)
        if plan["drop_above"]:
            dy = T["y"] - plan["top"]
            keep = [dict(e, y=e["y"] - dy) for e in keep]
        els = keep
        ctx = dict(ctx, items=plan["rows"])
    info = []                                    # one record per element
    for e in els:
        vis = check(e.get("show_if"), ctx)
        visible = True if vis is None else vis
        rec = {"e": e, "visible": visible, "h": e["h"], "src": False, "prims": None, "text": None}
        t = e["type"]
        if t in ("text", "field") and visible:
            text, hide = _text_value(e, ctx)
            bold = e.get("bold") or bool(check(e.get("bold_if"), ctx))
            lines = wrap(text, e["size"], bold, e["w"]) if text != "" and not hide else []
            if text.strip() == "" or hide:
                lines = []
            lh = lh_mm(e["size"])
            if not e["flow"]:
                lines = lines[:max(1, int(e["h"] / lh + 0.05))]
            rec.update(lines=lines, bold=bold, lh=lh, text=text)
            rec["h"] = len(lines) * lh if e["flow"] else e["h"]
            if e["flow"]:
                rec["src"] = True
            if not lines:
                rec["visible"] = False
        elif t == "table":
            if visible:
                prims, rec["h"], rec["head_h"], rec["row_hs"] = _table_layout(e, ctx)
                rec["prims"] = prims
            else:
                rec["h"] = 0
            rec["src"] = True
        elif not visible and e["flow"]:
            rec["h"], rec["src"] = 0, True
        if e["flow"] and t in ("text", "field") and not rec["visible"]:
            rec["src"] = True
        info.append(rec)

    # y positions: push-down with gap preservation
    order = sorted(range(len(info)), key=lambda i: (els[i]["y"], i))
    ynew, nb = {}, {}                            # new top; new bottom of sources
    for i in order:
        e, rec = els[i], info[i]
        cands = []
        for j in order:
            if j == i:
                continue
            s = els[j]
            if not info[j]["src"] or j not in nb:
                continue
            if s["y"] + s["h"] <= e["y"] + EPS and _overlap(e["x"], e["x"] + e["w"], s["x"], s["x"] + s["w"]):
                cands.append(j)
        live = [j for j in cands if not any(
            els[k]["y"] >= els[j]["y"] + els[j]["h"] - EPS and _overlap(els[j]["x"], els[j]["x"] + els[j]["w"], els[k]["x"], els[k]["x"] + els[k]["w"])
            for k in cands if k != j)]
        shift = max((nb[j] - (els[j]["y"] + els[j]["h"]) for j in live), default=0.0)
        ynew[i] = e["y"] + shift
        if rec["src"]:
            nb[i] = ynew[i] + rec["h"]
    # frames that enclose flowing elements grow with them
    for i, e in enumerate(els):
        if e["type"] in ("rect",) or (e["type"] == "line" and e["dir"] == "v"):
            inside = [j for j in nb if j != i and els[j]["y"] >= e["y"] - EPS and els[j]["y"] + els[j]["h"] <= e["y"] + e["h"] + EPS
                      and _overlap(e["x"], e["x"] + e["w"], els[j]["x"], els[j]["x"] + els[j]["w"])]
            if inside:
                bottom = max(nb[j] + (e["y"] + e["h"] - (els[j]["y"] + els[j]["h"])) for j in inside)
                info[i]["h"] = max(0.5, bottom - ynew[i])

    prims, bottom = [], 0.0
    for i, e in enumerate(els):
        rec, y = info[i], ynew[i]
        if not rec["visible"]:
            continue
        x, w, h, t = e["x"], e["w"], rec["h"], e["type"]
        bottom = max(bottom, y + h)
        if t in ("text", "field"):
            block = len(rec["lines"]) * rec["lh"]
            oy = {"top": 0, "middle": (h - block) / 2, "bottom": h - block}[e["valign"]] if not e["flow"] else 0
            prims.append({"t": "text", "x": x, "y": y + max(0, oy), "w": w, "lines": rec["lines"], "size": e["size"],
                          "bold": rec["bold"], "align": e["align"], "lh": rec["lh"]})
        elif t == "table":
            for p in rec["prims"]:
                q = dict(p)
                for kx in ("x", "x1", "x2"):
                    if kx in q:
                        q[kx] += x
                for ky in ("y", "y1", "y2"):
                    if ky in q:
                        q[ky] += y
                prims.append(q)
        elif t == "rect":
            prims.append({"t": "rect", "x": x, "y": y, "w": w, "h": h, "border": e["border"], "fill": e["fill"]})
        elif t == "line":
            if e["dir"] == "h":
                prims.append({"t": "line", "x1": x, "y1": y + h / 2, "x2": x + w, "y2": y + h / 2, "w": e["thick"]})
            else:
                prims.append({"t": "line", "x1": x + w / 2, "y1": y, "x2": x + w / 2, "y2": y + h, "w": e["thick"]})
        elif t == "image":
            data = _img_bytes(e["src"], ctx)
            if data:
                prims.append({"t": "img", "x": x, "y": y, "w": w, "h": h, "data": data})
        elif t in ("barcode", "qr"):
            value = get_path(ctx, e["field"]) if e["field"] else e["static"]
            if t == "barcode":
                th = lh_mm(e["size"]) if e["show_text"] else 0
                rects = barcode_rects(value, x, y, w, max(0.5, h - th))
                if rects:
                    prims.append({"t": "fill", "rects": rects})
                    if e["show_text"]:
                        prims.append({"t": "text", "x": x, "y": y + h - th, "w": w, "lines": [value], "size": e["size"],
                                      "bold": False, "align": "center", "lh": th})
            else:
                rects = qr_rects(value, x, y, w, h)
                if rects:
                    prims.append({"t": "fill", "rects": rects})
    ph = page["h"]
    final_h = max(10.0, bottom + 4) if page["auto_height"] else max(ph, bottom)
    meta = None
    main_id = plan["main"] if plan else None
    cand = [i for i, r in enumerate(info) if els[i]["type"] == "table" and r["visible"] and "row_hs" in r]
    if cand:
        mi = next((i for i in cand if els[i]["id"] == main_id), None) if main_id else min(cand, key=lambda i: els[i]["y"])
        if mi is not None:
            meta = {"id": els[mi]["id"], "y": ynew[mi], "h": info[mi]["h"], "head_h": info[mi]["head_h"],
                    "row_hs": info[mi]["row_hs"], "bottom": bottom}
    return {"w": page["w"], "h": round(final_h, 2), "prims": prims, "meta": meta}


def resolve(layout, ctx):
    """-> list of pages. Fixed-size pages whose main table overflows are split (table header repeats)."""
    page = layout["page"]
    first = _resolve_one(layout, ctx)
    meta = first.pop("meta")
    if page["auto_height"] or not meta or not meta["row_hs"] or first["h"] <= page["h"] + 0.01:
        return [first]
    PH, MB, MT = page["h"], 10.0, 10.0
    row_hs, n = meta["row_hs"], len(meta["row_hs"])
    foot = max(0.0, meta["bottom"] - (meta["y"] + meta["h"]))
    spans, i = [], 0
    while i < n:
        cap = (PH - MB - meta["y"] - meta["head_h"]) if not spans else (PH - MB - MT - meta["head_h"])
        s, j = 0.0, i
        while j < n and s + row_hs[j] <= cap + 1e-6:
            s += row_hs[j]
            j += 1
        j = max(j, i + 1)
        if j == n and sum(row_hs[i:j]) + foot > cap + 1e-6 and j - i > 1:
            j -= 1
        spans.append((i, j))
        i = j
    pages = []
    for k, (a, b) in enumerate(spans):
        plan = {"main": meta["id"], "rows": ctx["items"][a:b], "drop_above": k > 0, "drop_below": k < len(spans) - 1, "top": MT}
        pg = _resolve_one(layout, ctx, plan)
        pg.pop("meta")
        pages.append(pg)
    return pages


# ---------------------------------------------------------------- HTML
def _f(v):
    return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def render_page_html(pg):
    out = [f'<div class="page" style="width:{_f(pg["w"])}mm;height:{_f(pg["h"])}mm">']
    fills = []
    for p in pg["prims"]:
        t = p["t"]
        if t == "text":
            st = (f'left:{_f(p["x"])}mm;top:{_f(p["y"])}mm;width:{_f(p["w"])}mm;font-size:{_f(p["size"])}pt;'
                  f'line-height:{_f(p["lh"])}mm;text-align:{p["align"]};' + ("font-weight:bold;" if p["bold"] else ""))
            out.append(f'<div class="t" style="{st}">{"<br>".join(html.escape(l) or "&nbsp;" for l in p["lines"])}</div>')
        elif t == "rect":
            b = f'border:{_f(p["border"])}mm solid #000;' if p["border"] else ""
            bg = f'background:{p["fill"]};' if p["fill"] else ""
            out.append(f'<div class="r" style="left:{_f(p["x"])}mm;top:{_f(p["y"])}mm;width:{_f(p["w"])}mm;height:{_f(p["h"])}mm;{b}{bg}"></div>')
        elif t == "line":
            x1, y1, x2, y2, w = p["x1"], p["y1"], p["x2"], p["y2"], p["w"]
            if abs(y1 - y2) < 1e-6:
                st = f'left:{_f(min(x1, x2))}mm;top:{_f(y1 - w / 2)}mm;width:{_f(abs(x2 - x1))}mm;height:{_f(w)}mm'
            else:
                st = f'left:{_f(x1 - w / 2)}mm;top:{_f(min(y1, y2))}mm;width:{_f(w)}mm;height:{_f(abs(y2 - y1))}mm'
            out.append(f'<div class="l" style="{st}"></div>')
        elif t == "img":
            mime, box = _img_fit(p)
            out.append(f'<img class="i" style="left:{_f(box[0])}mm;top:{_f(box[1])}mm;width:{_f(box[2])}mm;height:{_f(box[3])}mm" '
                       f'src="data:{mime};base64,{base64.b64encode(p["data"]).decode()}">')
        elif t == "fill":
            fills.extend(p["rects"])
    if fills:
        d = "".join(f"M{_f(round(x, 3))} {_f(round(y, 3))}h{_f(round(w, 3))}v{_f(round(h, 3))}h-{_f(round(w, 3))}z" for x, y, w, h in fills)
        out.append(f'<svg class="s" viewBox="0 0 {_f(pg["w"])} {_f(pg["h"])}" width="{_f(pg["w"])}mm" height="{_f(pg["h"])}mm" '
                   f'shape-rendering="crispEdges"><path d="{d}" fill="#000"/></svg>')
    out.append("</div>")
    return "".join(out)


def _img_fit(p):
    """Keep aspect ratio, centred in the box. Returns (mime, (x,y,w,h))."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(p["data"]))
        iw, ih = im.size
        mime = Image.MIME.get(im.format, "image/png")
    except Exception:
        return "image/png", (p["x"], p["y"], p["w"], p["h"])
    k = min(p["w"] / iw, p["h"] / ih)
    w, h = iw * k, ih * k
    return mime, (p["x"] + (p["w"] - w) / 2, p["y"] + (p["h"] - h) / 2, w, h)


PAGE_CSS = """
@font-face{font-family:'PrintDV';src:url('/designer/font/regular/')}
@font-face{font-family:'PrintDV';font-weight:bold;src:url('/designer/font/bold/')}
.page{position:relative;background:#fff;overflow:hidden;font-family:'PrintDV','DejaVu Sans',Arial,sans-serif;color:#000;box-sizing:border-box}
.page .t{position:absolute;white-space:nowrap;overflow:visible}.page .r,.page .l,.page .i{position:absolute;box-sizing:border-box}
.page .l{background:#000}.page .s{position:absolute;left:0;top:0}
"""


# ---------------------------------------------------------------- PDF / images
def render_pdf(pages):
    ensure_fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setTitle("Друкована форма")
    for pg in pages:
        mm_ = 72 / 25.4
        c.setPageSize((pg["w"] * mm_, pg["h"] * mm_))
        for p in pg["prims"]:
            t = p["t"]
            if t == "text":
                c.setFont(font_name(p["bold"]), p["size"])
                for i, line in enumerate(p["lines"]):
                    base = p["y"] + i * p["lh"] + p["lh"] / 2 + p["size"] * PT * 0.34
                    yy = (pg["h"] - base) * mm_
                    if p["align"] == "center":
                        c.drawCentredString((p["x"] + p["w"] / 2) * mm_, yy, line)
                    elif p["align"] == "right":
                        c.drawRightString((p["x"] + p["w"]) * mm_, yy, line)
                    else:
                        c.drawString(p["x"] * mm_, yy, line)
            elif t == "rect":
                if p["fill"]:
                    c.setFillColor(p["fill"])
                c.setLineWidth(max(p["border"], 0.01) * mm_)
                c.rect(p["x"] * mm_, (pg["h"] - p["y"] - p["h"]) * mm_, p["w"] * mm_, p["h"] * mm_,
                       stroke=1 if p["border"] else 0, fill=1 if p["fill"] else 0)
                c.setFillColor("#000000")
            elif t == "line":
                c.setLineWidth(p["w"] * mm_)
                c.line(p["x1"] * mm_, (pg["h"] - p["y1"]) * mm_, p["x2"] * mm_, (pg["h"] - p["y2"]) * mm_)
            elif t == "img":
                _, (ix, iy, iw, ih) = _img_fit(p)
                try:
                    c.drawImage(ImageReader(io.BytesIO(p["data"])), ix * mm_, (pg["h"] - iy - ih) * mm_, iw * mm_, ih * mm_,
                                mask="auto")
                except Exception:
                    pass
            elif t == "fill":
                c.setFillColor("#000000")
                for x, y, w, h in p["rects"]:
                    c.rect(x * mm_, (pg["h"] - y - h) * mm_, w * mm_, h * mm_, stroke=0, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


def png_available():
    try:
        import pymupdf  # noqa: F401
        return True
    except ImportError:
        try:
            import fitz  # noqa: F401
            return True
        except ImportError:
            return False


def render_images(pdf_bytes, fmt="png", dpi=200):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        out.append(pix.tobytes("jpeg" if fmt == "jpg" else "png"))
    return out
