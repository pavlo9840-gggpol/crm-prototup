"""Оновити курси валют з НБУ (запускайте щодня за розкладом) і перерахувати прайси постачальників у валюті.

    python manage.py update_rates
"""
from django.core.management.base import BaseCommand

from core import currency


class Command(BaseCommand):
    help = "Оновити курси валют з НБУ"

    def handle(self, *a, **o):
        try:
            self.stdout.write("Курси НБУ оновлено: " + currency.refresh_from_nbu())
        except currency.RateError as e:
            self.stderr.write(str(e))
            raise SystemExit(1)
