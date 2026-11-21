from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0053_supplier_purchase_orders"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierClaim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("claim_number", models.CharField(blank=True, db_index=True, max_length=24)),
                ("kind", models.CharField(choices=[("shortage", "Недопоставка"), ("damaged", "Пошкодження"), ("wrong_item", "Не той товар"), ("quality", "Якість товару"), ("price", "Невідповідність ціни"), ("late", "Запізнення доставки"), ("other", "Інше")], max_length=16)),
                ("status", models.CharField(choices=[("open", "Чернетка"), ("sent", "Відправлено"), ("accepted", "Прийнято постачальником"), ("rejected", "Відхилено постачальником"), ("resolved", "Вирішено"), ("cancelled", "Скасовано")], db_index=True, default="open", max_length=12)),
                ("quantity", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ("requested_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("description", models.TextField()),
                ("supplier_response", models.TextField(blank=True)),
                ("resolution_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_claims", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_claims_created", to=settings.AUTH_USER_MODEL)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claims", to="core.purchaseorder")),
                ("order_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claims", to="core.purchaseorderitem")),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_claims_resolved", to=settings.AUTH_USER_MODEL)),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claims", to="core.supplier")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddIndex(model_name="supplierclaim", index=models.Index(fields=["company", "status", "created_at"], name="claim_company_status_idx")),
        migrations.AddIndex(model_name="supplierclaim", index=models.Index(fields=["company", "supplier", "created_at"], name="claim_company_supplier_idx")),
        migrations.AddConstraint(model_name="supplierclaim", constraint=models.CheckConstraint(condition=Q(quantity__isnull=True) | Q(quantity__gt=0), name="supplier_claim_qty_positive")),
        migrations.AddConstraint(model_name="supplierclaim", constraint=models.CheckConstraint(condition=Q(requested_amount__gte=0), name="supplier_claim_amount_nonnegative")),
    ]
