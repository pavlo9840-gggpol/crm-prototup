"""Best-effort conversion of v2 HTML print templates (DocTemplate) into designer layouts."""
import html
import re

from . import designer, docs

LEGACY_KINDS = {"receipt": "Касовий чек", "sale_invoice": "Видаткова накладна", "purchase": "Прихідна накладна"}
PAPERS = {"80mm": ("roll80", 80, 150, True), "58mm": ("roll58", 58, 120, True), "a4": ("a4", 210, 297, False)}
ACC = {"bank_name", "iban", "mfo", "holder"}


def _map_path(path, kind, filt=""):
    if path.startswith("a.") and path[2:] in ACC:
        return "account." + path[2:]
    if path == "company.logo_url":
        return "company.has_logo"
    if path == "doc.user" and kind in ("receipt", "sale_invoice"):
        return "doc.cashier"
    if path == "doc.customer" and "default" in filt:
        return "doc.customer_label"
    return path


def convert(body, paper, kind):
    """-> layout dict (unsanitised) built from the old template text."""
    keys_ok = docs.all_field_keys(kind)
    text = body
    loop_keys, has_loop = [], False
    m = re.search(r"\{%\s*for\s+(\w+)\s+in\s+items\s*%\}(.*?)\{%\s*endfor\s*%\}", text, re.S)
    if m:
        has_loop = True
        loop_keys = re.findall(r"\{\{\s*%s\.(\w+)" % re.escape(m.group(1)), m.group(2))
        if "forloop.counter" in m.group(2):
            loop_keys.insert(0, "n")
        text = text[:m.start()] + "\n@@TABLE@@\n" + text[m.end():]
    text = re.sub(r"\{%\s*if\s+([\w.]+)[^%]*%\}", lambda mm: f"\n@@IF:{_map_path(mm.group(1), kind)}@@\n", text)
    text = re.sub(r"\{%\s*(endif|else)\s*%\}", "\n@@ENDIF@@\n", text)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.S)
    text = re.sub(r"(?i)<img[^>]*logo[^>]*>", "\n@@LOGO@@\n", text)
    text = re.sub(r"(?i)<hr\s*/?>", "\n@@HR@@\n", text)
    text = re.sub(r'(?i)<(\w+)[^>]*class="[^"]*\bc\b[^"]*"[^>]*>', "\n@@C1@@\n", text)
    text = re.sub(r'(?i)<(\w+)[^>]*class="[^"]*\btot\b[^"]*"[^>]*>', "\n@@T1@@\n", text)
    text = re.sub(r"(?i)</(div|p|h\d)>", "\n@@C0@@\n", text)
    text = re.sub(r"(?i)<br\s*/?>|</tr>|</table>|</li>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "  ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)

    def tok(mm):
        p = _map_path(mm.group(1), kind, mm.group(2) or "")
        return "{{" + p + "}}" if p in keys_ok else ""
    text = re.sub(r"\{\{\s*([\w.]+)\s*(\|[^}]*)?\}\}", tok, text)

    roll = paper in ("80mm", "58mm")
    preset, W, H, auto = PAPERS.get(paper, PAPERS["a4"])
    m_ = 3 if roll else 10
    size = 8.5 if roll else 10
    lh = designer.lh_mm(size)
    els, y, n = [], float(m_), [0]

    def eid():
        n[0] += 1
        return f"c{n[0]}"

    def base(type_, x, y_, w, h, cond=None):
        return {"id": eid(), "type": type_, "x": x, "y": round(y_, 2), "w": w, "h": h, "group": "", "flow": True, "show_if": cond}
    cond_stack, center, total = [], False, False
    raw_lines = [l.strip() for l in text.split("\n") if l.strip()]
    # drop the old header row (<th> cells) that precedes the loop table: new table has its own header
    raw_lines = [l for i, l in enumerate(raw_lines)
                 if not (i + 1 < len(raw_lines) and raw_lines[i + 1] == "@@TABLE@@" and "{{" not in l and not l.startswith("@@"))]
    for line in raw_lines:
        if line.startswith("@@IF:"):
            p = line[5:-2]
            cond_stack.append({"field": p, "op": "filled", "value": ""} if p in keys_ok else None)
            continue
        if line == "@@ENDIF@@":
            if cond_stack:
                cond_stack.pop()
            continue
        if line == "@@C1@@":
            center = True
            continue
        if line == "@@T1@@":
            total = True
            continue
        if line == "@@C0@@":
            center = total = False
            continue
        cond = next((c for c in reversed(cond_stack) if c), None)
        if line == "@@LOGO@@":
            e = base("image", W / 2 - 18 if roll else m_, y, 36 if roll else 30, 14, {"field": "company.has_logo", "op": "filled", "value": ""})
            e["src"] = "company_logo"
            els.append(e)
            y += 15
        elif line == "@@HR@@":
            e = base("line", m_, y + 0.5, W - 2 * m_, 0.6, cond)
            e.update(dir="h", thick=0.2, flow=False)
            els.append(e)
            y += 2
        elif line == "@@TABLE@@":
            keys = [k for k in dict.fromkeys(loop_keys) if k in {c for c, _ in docs.DOC_TYPES[kind]["cols"]}] or ["name", "qty", "price", "total"]
            labels = dict(docs.DOC_TYPES[kind]["cols"])
            if roll:
                keys = [k for k in keys if k in ("name", "qty", "price", "total")] or keys
            weight = {"name": 3.0, "qty": 1.0, "price": 1.4, "total": 1.6, "n": 0.5, "unit": 0.9, "sku": 1.5}
            tot_w = sum(weight.get(k, 1.0) for k in keys)
            cols = [{"key": k, "title": labels[k], "w": round((W - 2 * m_) * weight.get(k, 1.0) / tot_w, 2),
                     "align": "left" if k in ("name", "sku") else "right", "wrap": k == "name"} for k in keys]
            e = base("table", m_, y, W - 2 * m_, 5 + 3 * 5.5, cond)
            e.update(cols=cols, row_h=5.5, head_h=5.5, head=True, border=not roll, size=size)
            els.append(e)
            y += 5.5 * 4 + 2
        else:
            e = base("text", m_, y, W - 2 * m_, lh + 0.2, cond)
            e.update(text=line, size=size + (2 if total else 0), bold=total, align="center" if center else ("right" if total else "left"),
                     valign="top", bold_if=None)
            els.append(e)
            y += lh + 0.6
    return {"format_version": designer.FORMAT_VERSION, "page": {"preset": preset, "w": W, "h": max(H if not auto else 60, round(y + 10)), "auto_height": auto},
            "elements": els}
