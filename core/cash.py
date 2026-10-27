"""Cash drawer maths. All amounts here are integers in kopecks."""
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

DENOMS = [100000, 50000, 20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50, 10]


def label(d):
    return str(d // 100) if d >= 100 else f"0.{d:02d}"


def to_kop(x):
    return int((Decimal(str(x)) * 100).to_integral_value(ROUND_HALF_UP))


def to_dec(kop):
    return Decimal(kop) / 100


def clean(counts):
    out = {}
    for k, v in (counts or {}).items():
        try:
            k, v = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if k in DENOMS and v > 0:
            out[k] = v
    return out


def store(counts):
    return {str(k): v for k, v in clean(counts).items()}


def total(counts):
    return sum(k * v for k, v in clean(counts).items())


def decompose(amount):
    """Customer's payment assumed as the largest notes first."""
    res = {}
    for d in DENOMS:
        n, amount = divmod(amount, d)
        if n:
            res[d] = n
    if amount:  # sub-10-kop remainder, keep as 10 kop coin
        res[10] = res.get(10, 0) + 1
    return res


def make_change(amount, avail):
    denoms = [d for d in DENOMS if avail.get(d, 0) > 0]
    calls = [0]

    @lru_cache(None)
    def dfs(i, rem):
        calls[0] += 1
        if rem == 0:
            return ()
        if i >= len(denoms) or calls[0] > 150000:
            return None
        d = denoms[i]
        for n in range(min(avail[d], rem // d), -1, -1):
            r = dfs(i + 1, rem - n * d)
            if r is not None:
                return ((d, n),) + r if n else r
        return None

    r = dfs(0, amount)
    return dict(r) if r is not None else None


def add(a, b, sign=1):
    res = dict(a)
    for k, v in b.items():
        res[k] = res.get(k, 0) + sign * v
        if res[k] == 0:
            del res[k]
    return res


def compute_payment(total_kop, payment, card_kop, received_kop, counts, drawer):
    """drawer: dict of counts or None (no shift => no drawer tracking)."""
    if payment == "card":
        card_kop = total_kop
    elif payment == "cash":
        card_kop = 0
    card_kop = max(0, min(card_kop, total_kop))
    cash_due = total_kop - card_kop
    res = {"ok": True, "msg": "", "cash_due": cash_due, "card": card_kop, "received": 0, "change": 0,
           "rounding": 0, "plan": {}, "recv_counts": {}, "drawer_after": drawer, "shortfall": 0}
    if cash_due == 0:
        return res
    if received_kop is None:
        received_kop = cash_due
    res["received"] = received_kop
    if received_kop < cash_due:
        res.update(ok=False, msg=f"Не вистачає {to_dec(cash_due - received_kop)} ₴")
        return res
    counts = clean(counts)
    recv_counts = counts if total(counts) == received_kop else decompose(received_kop)
    raw = received_kop - cash_due
    change = int((Decimal(raw) / 10).to_integral_value(ROUND_HALF_UP)) * 10  # round to 10 kop
    res.update(change=change, rounding=raw - change, recv_counts=recv_counts)
    if change == 0:
        if drawer is not None:
            res["drawer_after"] = add(drawer, recv_counts)
        return res
    if drawer is None:
        res["plan"] = decompose(change)
        return res
    avail = add(drawer, recv_counts)
    plan = make_change(change, avail)
    if plan is None:
        best = 0
        for k in range(1, 301):
            amt = change - k * 10
            if amt <= 0:
                break
            if make_change(amt, avail) is not None:
                best = amt
                break
        res.update(ok=False, shortfall=change - best,
                   msg=f"У касі немає розмінних коштів для здачі {to_dec(change)} ₴. "
                       f"Можна видати {to_dec(best)} ₴ — не вистачає {to_dec(change - best)} ₴. "
                       f"Попросіть менші купюри або оплату карткою.")
        return res
    res["plan"] = plan
    res["drawer_after"] = add(avail, plan, -1)
    return res
