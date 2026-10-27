"""Підтягування OE-кодів, описів і характеристик з карток товарів постачальників (для точних аналогів).

    python manage.py enrich_parts                  # усі постачальники з налаштованою карткою товару
    python manage.py enrich_parts --limit 2000     # за один запуск (за замовчуванням 2000)
Запускайте за розкладом: позиції, що вже оброблені, пропускаються, тож кожен запуск «доїдає» залишок.
BM Parts: ліміт 10 000 запитів на годину, тому 2000 за запуск безпечно.
"""
from django.core.management.base import BaseCommand

from core.apisync import SyncError
from core.enrich import enrich_batch
from core.models import Supplier


class Command(BaseCommand):
    help = "Підтягнути OE-коди й описи деталей з API постачальників"

    def add_arguments(self, parser):
        parser.add_argument("--supplier", type=int)
        parser.add_argument("--limit", type=int, default=2000)

    def handle(self, *a, **o):
        qs = Supplier.objects.select_related("api").filter(api__isnull=False).exclude(api__det_url="")
        if o["supplier"]:
            qs = qs.filter(pk=o["supplier"])
        failed = 0
        for sup in qs:
            try:
                done, errors = enrich_batch(sup, limit=o["limit"])
                self.stdout.write(f"{sup.name}: оброблено {done}, помилок {errors}")
            except SyncError as e:
                failed += 1
                self.stderr.write(f"{sup.name}: {e}")
        if not qs:
            self.stdout.write("Немає постачальників із налаштованою карткою товару")
        if failed:
            raise SystemExit(1)
