"""Застосовність деталей до авто: збіг марки/моделі й років випуску в рядках на кшталт «Renault Trafic II 2001-2014» чи «Opel Vivaro 01-»."""
import re

TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+")
YEARS = re.compile(r"(?<![\d.])(\d{4}|\d{2})\s*[-–—]\s*(\d{4}|\d{2})?(?![\d])")


def car_tokens(make, model):
    return [t for t in TOKEN.findall(f"{make or ''} {model or ''}".lower()) if len(t) >= 2]


def _year(s):
    y = int(s)
    return y if y >= 100 else (2000 + y if y < 50 else 1900 + y)


def year_range(line):
    """(від, до) з першого діапазону років у рядку; до = 9999, якщо діапазон відкритий («01-»). None, якщо років немає."""
    m = YEARS.search(line)
    if not m:
        return None
    a = _year(m.group(1))
    b = _year(m.group(2)) if m.group(2) else 9999
    return (a, b) if a <= b else None


def line_matches(line_lc, tokens, year=None):
    if not tokens or not all(t in line_lc for t in tokens):
        return False
    if year:
        yr = year_range(line_lc)
        if yr and not (yr[0] <= year <= yr[1]):
            return False
    return True
