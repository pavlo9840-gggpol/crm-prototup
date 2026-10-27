"""Демонстраційна база для перевірки всіх можливостей системи.

ВСІ ДАНІ ВИГАДАНІ: артикули й OE-номери не відповідають реальним каталогам, ціни умовні. Не використовуйте їх для замовлення запчастин.
Бренди й назви авто взято лише для реалістичності вигляду.

    python manage.py load_demo_data            # створити демо-дані (повторний запуск нічого не дублює)
    python manage.py load_demo_data --reset    # видалити демо-дані
Також записується файл тестової накладної demo_invoice.csv (для екрана «Приймання накладної»).
"""
import csv
import random
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import ProtectedError

from core import services
from core.models import (Category, CrossRef, Customer, CustomerCar, MarkupTier, PartInfo, PartOe, PayPlan, PriceRule, Product, Sale,
                         SaleItem, Supplier, SupplierItem, SupplierPriceChange, norm_sku)

TAG = "DEMO"
SUPPLIERS = ["Busmarket (демо)", "Омега-Авто (демо)"]
TYPES = [  # тип, позиції, база ціни, бренди
    ("Амортизатор", ["передній", "задній"], 1200, ["BILSTEIN", "SACHS", "KYB", "MONROE"]),
    ("Колодки гальмівні", ["передні", "задні"], 650, ["TRW", "ATE", "BOSCH", "FEBI"]),
    ("Тяга стабілізатора", ["лівий", "правий"], 320, ["MEYLE", "FEBI", "LEMFORDER", "TRW"]),
    ("Фільтр масляний", [""], 140, ["MANN-FILTER", "BOSCH", "FEBI", "MEYLE"]),
    ("Свічка запалювання", [""], 90, ["NGK", "BOSCH", "DENSO"]),
]
VEHICLES = [("Renault Trafic II", "01-", "Renault"), ("Opel Vivaro", "01-", "Opel"), ("Volkswagen Transporter T5", "03-", "Volkswagen"),
            ("Ford Transit", "06-", "Ford"), ("Fiat Ducato", "06-", "Fiat"), ("Mercedes Sprinter", "06-", "Mercedes")]
ART = {  # різні формати артикулів, щоб перевірити нормалізацію
    "BILSTEIN": lambda n: f"19-{n:06d}", "SACHS": lambda n: f"{n // 1000:03d} {n % 1000:03d}", "KYB": lambda n: f"3{n:05d}",
    "MONROE": lambda n: f"G{n:05d}", "TRW": lambda n: f"GDB{n:04d}", "ATE": lambda n: f"13.0460-{n % 10000:04d}.2",
    "BOSCH": lambda n: f"0 986 {n:06d}", "FEBI": lambda n: f"{n:05d}", "MEYLE": lambda n: f"16-16 060 {n % 10000:04d}",
    "LEMFORDER": lambda n: f"{n:05d} 01", "MANN-FILTER": lambda n: f"W {n % 900 + 100}/{n % 90 + 10}", "NGK": lambda n: f"BKR{n % 90 + 10}E",
    "DENSO": lambda n: f"K{n % 90 + 10}PR-U"}


class Command(BaseCommand):
    help = "Створити (або видалити з --reset) демонстраційну базу з ВИГАДАНИХ даних"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true")
        parser.add_argument("--out", default="demo_invoice.csv", help="куди записати тестову накладну")

    def handle(self, *a, **o):
        if o["reset"]:
            self.reset()
            self.stdout.write("Демо-дані видалено.")
            return
        if Supplier.objects.filter(name__in=SUPPLIERS).exists():
            self.stdout.write("Демо-дані вже створено. Щоб створити заново: load_demo_data --reset")
            return
        with transaction.atomic():
            stats = self.build(o["out"])
        self.stdout.write("Готово. " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
        self.stdout.write(self.style.WARNING("Усі артикули й OE-номери вигадані: це тестові дані, а не реальний каталог."))

    # ------------------------------------------------------------------
    def reset(self):
        demo_cust = Customer.objects.filter(notes=TAG)
        sales = Sale.objects.filter(customer__in=demo_cust)
        SaleItem.objects.filter(sale__in=sales).delete()
        sales.delete()
        PartOe.objects.filter(source=TAG).delete()
        PartInfo.objects.filter(source=TAG).delete()
        CrossRef.objects.filter(source=TAG).delete()
        demo_cust.delete()
        for qs, what in ((Product.objects.filter(notes=TAG), "товари"), (Supplier.objects.filter(name__in=SUPPLIERS), "постачальників")):
            try:
                with transaction.atomic():
                    qs.delete()
            except ProtectedError:      # на них уже посилаються справжні закупівлі (напр. прийнята тестова накладна)
                self.stdout.write(self.style.WARNING(f"Частину демо-даних ({what}) не видалено: є проведені закупівлі. "
                                                     f"Видаліть їх вручну або залиште."))
        PriceRule.objects.filter(name__endswith="(демо)").delete()
        PayPlan.objects.filter(note=TAG).delete()
        Category.objects.filter(name__endswith="(демо)").delete()

    def build(self, out_path):
        rnd = random.Random(42)
        User = get_user_model()
        # демо-замовлення оформлює менеджер (не власник), щоб на його прикладі було видно «Мої замовлення» й комісію
        user = (User.objects.filter(is_staff=False, is_active=True).first() or User.objects.filter(is_superuser=True).first()
                or User.objects.create_superuser("demo", "", "demo12345"))
        if not MarkupTier.objects.exists():
            MarkupTier.objects.create(name="Роздріб", percent=30, is_default=True)
            MarkupTier.objects.create(name="СТО", percent=15)
            MarkupTier.objects.create(name="Оптовик", percent=8)
        sup = [Supplier.objects.create(name=n) for n in SUPPLIERS]
        PriceRule.objects.create(name="Загальне (демо)", markup_pct=30, min_margin_pct=15, rounding=10, priority=100)
        PriceRule.objects.create(name="Bilstein (демо)", brand="BILSTEIN", markup_pct=40, min_margin_pct=20, rounding=10, priority=50)
        PayPlan.objects.create(user=None, margin_pct=10, fixed_monthly=0, note=TAG)
        cats = {t[0]: Category.objects.create(name=f"{t[0]} (демо)") for t in TYPES}
        n_parts = n_oe = n_stock = n_noOE = 0
        counter = 10000
        parts = []                                            # (brand, sku, name, cost, group_key)
        for ti, (typ, positions, base, brands) in enumerate(TYPES):
            for vi, (veh, years, _mk) in enumerate(VEHICLES):
                for pi, pos in enumerate(positions):
                    oe = f"DM{ti}{vi}{pi}-{rnd.randint(1000, 9999)}"
                    has_oe = rnd.random() > 0.2                # 20% груп без OE: перевірка виведення за назвою
                    for bi, brand in enumerate(brands):
                        counter += rnd.randint(7, 211)
                        sku = ART[brand](counter)
                        style = bi % 3                          # різний стиль назв у різних брендів
                        p = f" ({pos})" if pos and style == 0 else (f" {pos}" if pos else "")
                        name = f"{typ}{p} {veh} {years}" if style != 2 else f"{typ}{p} {veh}"
                        cost = Decimal(base * rnd.uniform(0.7, 1.4) * (1 + vi * 0.02)).quantize(Decimal("0.01"))
                        parts.append((brand, sku, name, cost, (ti, vi, pi), oe if has_oe else None, typ, pos, veh))
        own = set(rnd.sample(range(len(parts)), k=len(parts) // 7))
        for idx, (brand, sku, name, cost, grp, oe, typ, pos, veh) in enumerate(parts):
            sn = norm_sku(sku)
            n_parts += 1
            for k, s in enumerate(sup):
                if k == 1 and rnd.random() > 0.6:
                    continue
                c = (cost * Decimal(rnd.uniform(0.95, 1.08))).quantize(Decimal("0.01"))
                avail = rnd.random() > 0.08
                SupplierItem.objects.create(supplier=s, brand=brand, sku=sku, sku_norm=sn, name=name, cost=c,
                                            qty_avail=Decimal(rnd.randint(1, 12)) if avail else Decimal(0), available=avail,
                                            ext_id=f"demo-{s.pk}-{idx}")
            if oe:
                PartOe.objects.create(brand=brand, sku_norm=sn, sku=sku, oe_norm=norm_sku(oe), oe_raw=oe, oe_brand=veh.split()[0].upper(), source=TAG)
                n_oe += 1
            else:
                n_noOE += 1
            PartInfo.objects.create(brand=brand, sku_norm=sn, sku=sku, source=TAG,
                                    description=f"{typ} {brand} для {veh}. Тестовий опис (вигадані дані).",
                                    attrs=f"Позиція: {pos or 'універсальна'}\nВиробник: {brand}",
                                    cars=f"{veh} {VEHICLES[grp[1]][1]}")
            if idx in own:
                bad = n_stock % 4 == 0                          # кожна 4-та: ціна просіла нижче мінімальної маржі (для екрана «Перегляд цін»)
                Product.objects.create(name=name, sku=sku, brand=brand, category=cats[typ], cost_price=cost, auto_price=(n_stock % 2 == 1),
                                       price=(cost * Decimal("1.05")).quantize(Decimal("0.01")) if bad else services.markup_price(cost, 30),
                                       current_stock=Decimal(rnd.randint(0, 6)), notes=TAG)
                n_stock += 1
        # кілька змін закупівельної ціни в прайсі (для журналу на екрані «Перегляд цін»)
        for prod in list(Product.objects.filter(notes=TAG)[:10]):
            it = SupplierItem.objects.filter(supplier=sup[0], brand=prod.brand.upper(), sku_norm=prod.sku_norm).first()
            if it:
                SupplierPriceChange.objects.create(item=it, old_cost=(it.cost * Decimal("0.92")).quantize(Decimal("0.01")), new_cost=it.cost)
        # кілька кросів «від постачальника» поверх OE (для довіри з кількох джерел)
        firsts = [p for p in parts if p[5]][:20]
        for a, b in zip(firsts[::2], firsts[1::2]):
            if a[4] == b[4] and norm_sku(a[1]) != norm_sku(b[1]):
                x, y = sorted((norm_sku(a[1]), norm_sku(b[1])))
                CrossRef.objects.get_or_create(a=x, b=y, defaults={"a_raw": a[1], "b_raw": b[1], "source": TAG})
        # клієнти з авто
        t_sto = MarkupTier.objects.filter(name="СТО").first()
        ivan = Customer.objects.create(name="Іван Петренко (демо)", phone="0500000001", notes=TAG)
        sto = Customer.objects.create(name="СТО «Вікторія» (демо)", phone="0500000002", notes=TAG, markup_tier=t_sto)
        olena = Customer.objects.create(name="Олена Коваль (демо)", phone="0500000003", notes=TAG)
        car_i = CustomerCar.objects.create(customer=ivan, plate="AA 1234 BB", make="Renault", model="Trafic II", year=2008)
        CustomerCar.objects.create(customer=sto, plate="KA 5555 CE", make="Opel", model="Vivaro", year=2011)
        CustomerCar.objects.create(customer=sto, plate="AE 7777 AA", make="Ford", model="Transit", year=2014)
        car_o = CustomerCar.objects.create(customer=olena, plate="BC 4321 AK", make="Fiat", model="Ducato", year=2012)
        # два замовлення «під замовлення» у Busmarket, щоб протестувати приймання накладної
        invoice = []
        orders = []
        for cust, car, ti, vi, pi in ((ivan, car_i, 0, 0, 1), (olena, car_o, 1, 4, 0)):
            si = (SupplierItem.objects.filter(supplier=sup[0], available=True, name__startswith=TYPES[ti][0], name__contains=VEHICLES[vi][0])
                  .order_by("pk").first())
            if si:
                prod = services.ensure_product(si)
                prod.notes = TAG
                prod.save(update_fields=["notes"])
                price = services.markup_price(si.cost, 30)
                services.finalize_sale(user, [(prod, Decimal(2), price, {"si": si, "list": price})], "cash", cust, None, car=car)
                orders.append(si)
                invoice.append([si.brand, si.sku, si.name, 2, si.cost])
        extra = SupplierItem.objects.filter(supplier=sup[0], available=True).exclude(pk__in=[s.pk for s in orders]).order_by("pk")[40]
        invoice.append([extra.brand, extra.sku, extra.name, 3, extra.cost])
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Бренд", "Артикул", "Назва", "Кількість", "Ціна"])
            w.writerows(invoice)
        self.stdout.write(f"Тестову накладну записано: {out_path} (постачальник: {SUPPLIERS[0]})")
        return {"деталей": n_parts, "OE-звʼязків": n_oe, "груп без OE (лише за назвою)": n_noOE, "позицій на складі": n_stock,
                "рядків у прайсах": SupplierItem.objects.filter(supplier__in=sup).count(), "замовлень": len(orders)}
