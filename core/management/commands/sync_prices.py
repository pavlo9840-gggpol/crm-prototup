"""Оновлення каталогів постачальників через API. Запуск за розкладом (cron / Планувальник завдань Windows):

    python manage.py sync_prices                 # усі постачальники з налаштованим API
    python manage.py sync_prices --supplier 3    # один постачальник
    python manage.py sync_prices --force         # навіть якщо позицій стало суттєво менше
"""
from django.core.management.base import BaseCommand

from core.apisync import SyncError, sync_supplier
from core.models import Supplier


class Command(BaseCommand):
    help = "Оновити каталоги постачальників через їхній API"

    def add_arguments(self, parser):
        parser.add_argument("--supplier", type=int)
        parser.add_argument("--force", action="store_true")

    def handle(self, *a, **o):
        qs = Supplier.objects.select_related("api").filter(api__isnull=False).exclude(api__url="")
        if o["supplier"]:
            qs = qs.filter(pk=o["supplier"])
        failed = 0
        for sup in qs:
            try:
                self.stdout.write(f"{sup.name}: {sync_supplier(sup, None, o['force'])}")
            except SyncError as e:
                failed += 1
                self.stderr.write(f"{sup.name}: ПОМИЛКА: {e}")
        if not qs:
            self.stdout.write("Немає постачальників із налаштованим API")
        if failed:
            raise SystemExit(1)
