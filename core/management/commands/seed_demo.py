from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from core.models import Category, Product, Supplier


class Command(BaseCommand):
    help = "Create demo users (admin/admin123, cashier/cashier123), categories, a supplier and products"

    def handle(self, *args, **opts):
        U = get_user_model()
        if not U.objects.filter(username="admin").exists():
            U.objects.create_superuser("admin", "", "admin123")
        if not U.objects.filter(username="cashier").exists():
            U.objects.create_user("cashier", password="cashier123")
        drinks, _ = Category.objects.get_or_create(name="Напої")
        soda, _ = Category.objects.get_or_create(name="Газовані напої", parent=drinks)
        Supplier.objects.get_or_create(name="Demo Supplier")
        Product.objects.get_or_create(sku="COLA05", defaults=dict(
            name="Кола 0.5л", barcode="4820000000011", category=soda, unit="пляшка",
            cost_price=20, price=30, min_stock=150, is_favorite=True))
        self.stdout.write(self.style.SUCCESS("Demo data ready"))
