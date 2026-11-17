from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Запустити локальний вебсервер АвтоЗапас на Waitress (без dev runserver)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1", help="Адреса прослуховування; за замовчуванням лише цей ПК.")
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument("--threads", type=int, default=8)

    def handle(self, *args, **options):
        if not 1 <= options["port"] <= 65535:
            raise CommandError("Порт має бути в діапазоні 1–65535.")
        if not 1 <= options["threads"] <= 128:
            raise CommandError("Кількість потоків має бути в діапазоні 1–128.")
        try:
            from waitress import serve
        except ImportError as exc:
            raise CommandError(
                "Waitress не встановлений. Виконайте: pip install -r requirements.txt"
            ) from exc

        from config.wsgi import application

        self.stdout.write(self.style.SUCCESS(
            f"АвтоЗапас запущено через Waitress: http://{options['host']}:{options['port']}/"
        ))
        self.stdout.write("Для зупинки натисніть Ctrl+C. Для доступу з LAN окремо налаштуйте firewall.")
        serve(
            application,
            host=options["host"],
            port=options["port"],
            threads=options["threads"],
            ident="AutoZapaz",
        )
