"""Single local-store initialization helpers (not SaaS workspace creation)."""
from django.db import transaction
from .models import Company, CompanyMembership


@transaction.atomic
def create_local_store(owner, name):
    """Create the one store for this installation and assign its local owner role."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Вкажіть назву магазину.")

    stores = Company.objects.select_for_update().order_by("pk")
    if stores.count() > 1:
        raise ValueError("У базі кілька старих магазинів. Спочатку потрібне контрольоване об'єднання даних.")
    if stores.exists():
        store = stores.first()
        if store.owner_id and store.owner_id != owner.pk:
            raise ValueError("Локальний магазин уже має власника. Не можна непомітно змінити власника інсталяції.")
        if store.owner_id and store.name != clean_name:
            raise ValueError("Локальний магазин уже налаштований. Зміну назви виконайте в налаштуваннях магазину.")
        store.name = clean_name
        store.owner = owner
        store.save(update_fields=["name", "owner", "updated_at"])
    else:
        slug = "local-autozapaz"
        store = Company.objects.create(
            name=clean_name,
            slug=slug,
            owner=owner,
        )

    CompanyMembership.objects.update_or_create(
        company=store,
        user=owner,
        defaults={"role": "owner", "is_active": True},
    )
    return store
