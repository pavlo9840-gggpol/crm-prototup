"""Standard (built-in) print forms. They are read-only in the UI and re-synced from here on every update;
users create copies (custom templates), which are never overwritten."""
from .designer import FORMAT_VERSION

_n = [0]


def _id():
    _n[0] += 1
    return f"b{_n[0]}"


def E(type_, x, y, w, h, **kw):
    return {"id": _id(), "type": type_, "x": x, "y": y, "w": w, "h": h, "group": "", "flow": False, "show_if": None, **kw}


def text(x, y, w, h, t, size=9, bold=False, align="left", **kw):
    return E("text", x, y, w, h, text=t, size=size, bold=bold, align=align, valign="top", bold_if=None, **kw)


def field(x, y, w, h, key, prefix="", suffix="", size=9, bold=False, align="left", fmt="text", empty="show", valign="top", **kw):
    return E("field", x, y, w, h, field=key, prefix=prefix, suffix=suffix, size=size, bold=bold, align=align, valign=valign,
             format=fmt, empty=empty, bold_if=None, **kw)


def hline(x, y, w, thick=0.2, **kw):
    return E("line", x, y, w, 0.6, dir="h", thick=thick, **kw)


def table(x, y, w, h, cols, size=9, row_h=6, head_h=6, border=True, **kw):
    return E("table", x, y, w, h, cols=[{"key": k, "title": t, "w": cw, "align": a, "wrap": wr} for k, t, cw, a, wr in cols],
             row_h=row_h, head_h=head_h, head=True, border=border, size=size, **kw)


def layout(preset, w, h, auto, els):
    return {"format_version": FORMAT_VERSION, "page": {"preset": preset, "w": w, "h": h, "auto_height": auto}, "elements": els}


HAS_LOGO = {"field": "company.has_logo", "op": "filled", "value": ""}


def receipt(width):
    _n[0] = 0
    m, cw = 3, width - 6
    if width >= 80:
        cols = [("name", "Назва", 36, "left", True), ("qty", "К-сть", 10, "right", False),
                ("price", "Ціна", 13, "right", False), ("total", "Сума", 15, "right", False)]
    else:
        cols = [("name", "Назва", 22, "left", True), ("qty", "К-сть", 8, "right", False),
                ("price", "Ціна", 10, "right", False), ("total", "Сума", 12, "right", False)]
    els = [
        E("image", width / 2 - 18, 2, 36, 14, src="company_logo", flow=True, show_if=HAS_LOGO),
        field(m, 17, cw, 5, "company.name", size=11, bold=True, align="center", flow=True),
        field(m, 22, cw, 4, "company.address", size=8, align="center", flow=True),
        field(m, 26, cw, 4, "company.phone", prefix="Тел.: ", size=8, align="center", flow=True),
        field(m, 30, cw, 4, "company.tax_id", prefix="ЄДРПОУ/ІПН: ", size=8, align="center", flow=True),
        field(m, 34, cw, 4, "company.receipt_header", size=8, align="center", flow=True),
        hline(m, 39, cw),
        field(m, 41, cw, 4.5, "doc.number", prefix="Чек № ", size=9, bold=True),
        field(m, 45.5, cw, 4.5, "doc.date", size=8),
        field(m, 50, cw, 4.5, "doc.cashier", prefix="Касир: ", size=8),
        field(m, 54.5, cw, 4.5, "doc.customer", prefix="Клієнт: ", size=8, flow=True),
        hline(m, 60, cw),
        table(m, 62, cw, 20, cols, size=8, row_h=5, head_h=5, border=False),
        hline(m, 84, cw),
        text(m, 86, cw * 0.45, 6, "РАЗОМ:", size=11, bold=True),
        field(m + cw * 0.4, 86, cw * 0.6, 6, "doc.total", suffix=" ₴", size=11, bold=True, align="right"),
        field(m, 92.5, cw, 4.5, "doc.vat", prefix="в т.ч. ПДВ: ", suffix=" ₴", size=8, align="right", flow=True,
              show_if={"field": "doc.vat", "op": "filled", "value": ""}),
        field(m, 97, cw, 4.5, "doc.payment", prefix="Оплата: ", size=8, flow=True),
        field(m, 101.5, cw, 4.5, "doc.cash_received", prefix="Отримано готівкою: ", suffix=" ₴", size=8, flow=True,
              show_if={"field": "doc.cash_received", "op": "filled", "value": ""}),
        field(m, 106, cw, 4.5, "doc.change", prefix="Здача: ", suffix=" ₴", size=9, bold=True, flow=True,
              show_if={"field": "doc.cash_received", "op": "filled", "value": ""}),
        field(m, 110.5, cw, 4.5, "doc.card_amount", prefix="Карткою: ", suffix=" ₴", size=8, flow=True,
              show_if={"field": "doc.card_amount", "op": "filled", "value": ""}),
        hline(m, 116, cw),
        E("qr", width / 2 - 13, 118, 26, 26, field="doc.qr_value"),
        field(m, 146, cw, 4, "company.receipt_footer", size=8, align="center", flow=True),
    ]
    return layout("roll80" if width >= 80 else "roll58", width, 155, True, els)


def _party_blocks(left_title, left, right_title, right):
    els = [text(10, 34, 92, 5, left_title, bold=True, size=10), text(108, 34, 92, 5, right_title, bold=True, size=10)]
    y = 39.5
    for kind, key, pre in left:
        els.append(field(10, y, 92, 5, key, prefix=pre, size=9, flow=True, bold=(kind == "b")))
        y += 5
    y = 39.5
    for kind, key, pre in right:
        els.append(field(108, y, 92, 5, key, prefix=pre, size=9, flow=True, bold=(kind == "b")))
        y += 5
    return els


COLS_A4 = [("n", "№", 9, "center", False), ("sku", "Код", 24, "left", False), ("name", "Назва", 77, "left", True),
           ("unit", "Од.", 14, "center", False), ("qty", "К-сть", 20, "right", False),
           ("price", "Ціна", 22, "right", False), ("total", "Сума", 24, "right", False)]


def _totals(y, label="Разом:", vat=True):
    els = [text(120, y, 40, 6, label, bold=True, size=11, align="right"),
           field(160, y, 40, 6, "doc.total", suffix=" ₴", bold=True, size=11, align="right")]
    if vat:
        els.append(field(120, y + 6.5, 80, 5, "doc.vat", prefix="у т.ч. ПДВ: ", suffix=" ₴", size=9, align="right", flow=True,
                         show_if={"field": "doc.vat", "op": "filled", "value": ""}))
    return els


def header(title_prefix):
    return [E("image", 10, 8, 30, 18, src="company_logo", show_if=HAS_LOGO),
            field(10, 10, 190, 8, "doc.number", prefix=title_prefix, size=16, bold=True, align="center"),
            field(10, 19, 190, 6, "doc.date", prefix="від ", size=10, align="center"),
            hline(10, 29, 190, 0.4)]


def sale_invoice(short=False):
    _n[0] = 0
    els = header("Видаткова накладна № ")
    if short:
        els += [text(10, 34, 40, 5, "Покупець:", bold=True), field(40, 34, 160, 5, "doc.customer_label", size=10, flow=True)]
        ty = 42
    else:
        els += _party_blocks("Постачальник:", [("b", "company.name", ""), ("", "company.legal_name", ""), ("", "company.address", ""),
                                               ("", "company.phone", "Тел.: "), ("", "company.tax_id", "ЄДРПОУ/ІПН: ")],
                             "Покупець:", [("b", "doc.customer_label", ""), ("", "doc.customer_phone", "Тел.: "),
                                           ("", "doc.payment", "Оплата: ")])
        ty = 68
    els.append(table(10, ty, 190, 36, COLS_A4, size=9, row_h=6, head_h=6.5))
    els += _totals(ty + 40)
    if not short:
        pay_y = ty + 56
        els += [text(10, pay_y, 100, 5, "Реквізити для оплати:", bold=True, size=9, show_if={"field": "account.iban", "op": "filled", "value": ""}, flow=True),
                field(10, pay_y + 5, 190, 5, "account.iban", prefix="IBAN: ", size=9, flow=True),
                field(10, pay_y + 10, 190, 5, "account.bank_name", prefix="Банк: ", size=9, flow=True),
                field(10, pay_y + 15, 190, 5, "account.holder", prefix="Отримувач: ", size=9, flow=True)]
        sy = pay_y + 30
    else:
        sy = ty + 60
    els += [text(10, sy, 92, 6, "Відпустив: ______________________", size=10), text(108, sy, 92, 6, "Отримав: ______________________", size=10)]
    return layout("a4", 210, 297, False, els)


def purchase():
    _n[0] = 0
    els = header("Прихідна накладна № ")
    els += _party_blocks("Постачальник:", [("b", "doc.supplier", ""), ("", "doc.supplier_phone", "Тел.: ")],
                         "Отримувач:", [("b", "company.name", ""), ("", "company.legal_name", ""), ("", "company.address", ""),
                                        ("", "company.tax_id", "ЄДРПОУ/ІПН: ")])
    els.append(table(10, 62, 190, 36, COLS_A4, size=9, row_h=6, head_h=6.5))
    els += _totals(102)
    els += [text(10, 130, 92, 6, "Здав: ______________________", size=10),
            field(108, 130, 92, 6, "doc.user", prefix="Прийняв (", suffix="): ____________________", size=10)]
    return layout("a4", 210, 297, False, els)


def writeoff():
    _n[0] = 0
    els = header("Акт № ")
    els += [text(10, 33, 190, 6, "Акт списання / коригування запасів", size=11, bold=True, align="center"),
            field(10, 41, 190, 5, "doc.kind", prefix="Тип операції: ", size=10, flow=True),
            field(10, 46, 190, 5, "doc.reason", prefix="Причина: ", size=10, flow=True),
            field(10, 51, 190, 5, "doc.note", prefix="Примітка: ", size=10, flow=True),
            table(10, 60, 190, 18, [("n", "№", 9, "center", False), ("sku", "Код", 26, "left", False), ("name", "Назва", 80, "left", True),
                                    ("unit", "Од.", 14, "center", False), ("qty", "К-сть", 20, "right", False),
                                    ("price", "Собівартість", 21, "right", False), ("total", "Сума", 20, "right", False)],
                  size=9, row_h=6, head_h=6.5)]
    els += _totals(82, vat=False)
    els += [text(10, 110, 92, 6, "Відповідальна особа: ____________________", size=10),
            field(108, 110, 92, 6, "doc.user", prefix="Склав: ", size=10)]
    return layout("a4", 210, 297, False, els)


def stock_report():
    _n[0] = 0
    cols = [("n", "№", 8, "center", False), ("sku", "Код", 22, "left", False), ("name", "Назва", 56, "left", True),
            ("category", "Категорія", 26, "left", True), ("unit", "Од.", 10, "center", False), ("qty", "Залишок", 17, "right", False),
            ("min", "Мін.", 12, "right", False), ("cost", "Закупівля", 17, "right", False), ("value", "Вартість", 22, "right", False)]
    els = [E("image", 10, 8, 30, 14, src="company_logo", show_if=HAS_LOGO),
           text(10, 10, 190, 8, "Звіт про залишки товарів", size=16, bold=True, align="center"),
           field(10, 19, 190, 5, "doc.date", prefix="Станом на ", size=10, align="center"),
           field(10, 25, 190, 5, "doc.filter", prefix="Фільтри: ", size=9, align="center", flow=True),
           table(10, 33, 190, 20, cols, size=8, row_h=5.5, head_h=6.5)]
    els += [text(100, 57, 60, 6, "Вартість запасів:", bold=True, size=11, align="right"),
            field(160, 57, 40, 6, "doc.total_value", suffix=" ₴", bold=True, size=11, align="right"),
            field(10, 57, 80, 5, "doc.count", prefix="Позицій: ", size=9)]
    return layout("a4", 210, 297, False, els)


def movement_report():
    _n[0] = 0
    cols = [("n", "№", 9, "center", False), ("date", "Дата", 33, "left", False), ("sku", "Код", 24, "left", False),
            ("name", "Товар", 72, "left", True), ("type", "Тип", 40, "left", False), ("change", "Зміна", 20, "right", False),
            ("balance", "Залишок", 22, "right", False), ("doc_ref", "Документ", 29, "left", False), ("user", "Оператор", 28, "left", False)]
    els = [E("image", 10, 8, 30, 14, src="company_logo", show_if=HAS_LOGO),
           text(10, 10, 277, 8, "Звіт про рух товарів", size=16, bold=True, align="center"),
           field(10, 19, 277, 5, "doc.period", prefix="Період: ", size=10, align="center"),
           table(10, 28, 277, 20, cols, size=8, row_h=5.5, head_h=6.5),
           field(10, 52, 120, 5, "doc.count", prefix="Записів: ", size=9),
           field(150, 52, 137, 5, "doc.date", prefix="Надруковано: ", size=9, align="right")]
    return layout("a4l", 297, 210, False, els)


def pricetag():
    _n[0] = 0
    els = [field(3, 2, 52, 11.5, "doc.name", size=9, bold=True, align="center", valign="middle"),
           field(3, 14, 52, 11, "doc.price", suffix=" ₴", size=22, bold=True, align="center"),
           E("barcode", 4, 26.5, 50, 10.5, field="doc.barcode", show_text=True, size=7),
           field(3, 37, 52, 3, "doc.unit", prefix="за ", size=6, align="center")]
    return layout("tag58", 58, 40, False, els)


BUILTINS = [
    # (key, doc_type, name, layout factory)
    ("receipt-80", "receipt", "Чек 80 мм (стандартний)", lambda: receipt(80)),
    ("receipt-58", "receipt", "Чек 58 мм", lambda: receipt(58)),
    ("sale_invoice-std", "sale_invoice", "Видаткова накладна (стандартна)", lambda: sale_invoice(False)),
    ("sale_invoice-short", "sale_invoice", "Видаткова накладна (скорочена)", lambda: sale_invoice(True)),
    ("purchase-std", "purchase", "Прихідна накладна (стандартна)", purchase),
    ("writeoff-std", "writeoff", "Акт списання (стандартний)", writeoff),
    ("stock_report-std", "stock_report", "Звіт про залишки (стандартний)", stock_report),
    ("movement_report-std", "movement_report", "Звіт про рух товарів (стандартний)", movement_report),
    ("pricetag-std", "pricetag", "Цінник 58×40 мм (стандартний)", pricetag),
]
