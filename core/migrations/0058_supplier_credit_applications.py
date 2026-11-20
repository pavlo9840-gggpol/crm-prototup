from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0057_supplier_compensation_void"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierCreditApplication",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Сума заліку")),
                ("note", models.CharField(blank=True, max_length=250, verbose_name="Примітка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("is_void", models.BooleanField(db_index=True, default=False, verbose_name="Скасовано")),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("void_reason", models.CharField(blank=True, max_length=250)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_credit_applications", to="core.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_credit_applications_created", to=settings.AUTH_USER_MODEL)),
                ("source_purchase", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="credit_applications_out", to="core.purchase")),
                ("target_purchase", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="credit_applications_in", to="core.purchase")),
                ("voided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_credit_applications_voided", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddIndex(model_name="suppliercreditapplication", index=models.Index(fields=["company", "source_purchase"], name="sup_credit_src_idx")),
        migrations.AddIndex(model_name="suppliercreditapplication", index=models.Index(fields=["company", "target_purchase"], name="sup_credit_dst_idx")),
        migrations.AddConstraint(model_name="suppliercreditapplication", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="supplier_credit_application_positive")),
    ]
