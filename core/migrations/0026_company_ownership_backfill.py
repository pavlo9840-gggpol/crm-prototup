from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


BUSINESS_MODELS = ["Category","Supplier","Product","PriceHistory","Customer","Shift","Sale","SaleItem","SaleReturn","SaleReturnItem","Purchase","PurchaseItem","StockAdjustment","StockMovement","CashOperation","CompanySettings","BankAccount","DocTemplate","PrintTemplate","SupplierItem","PriceImport","CrossRef","MarkupTier","SupplierApi","PartOe","PartInfo","CrossReject","CustomerCar","Receipt","PriceRule","SupplierPriceChange","PayPlan","Shipment","ServiceAppointment","ServiceOrder","ServiceOrderLine","ServicePayment"]


def assign_legacy_company(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    alias = schema_editor.connection.alias
    owner = User.objects.using(alias).filter(is_active=True).order_by("pk").first()
    company, _ = Company.objects.using(alias).get_or_create(
        slug="legacy-autozapaz",
        defaults={
            "name": "Основний магазин (перенесені дані)",
            "owner_id": owner.pk if owner else None,
            "plan": "pro",
            "status": "active",
        },
    )
    Membership = apps.get_model("core", "CompanyMembership")
    for user in User.objects.using(alias).filter(is_active=True).order_by("pk"):
        role = "owner" if owner and user.pk == owner.pk else ("admin" if user.is_staff else "cashier")
        Membership.objects.using(alias).get_or_create(
            company_id=company.pk, user_id=user.pk,
            defaults={"role": role, "is_active": True},
        )

    for model_name in BUSINESS_MODELS:
        Model = apps.get_model("core", model_name)
        Model.objects.using(alias).filter(company__isnull=True).update(company_id=company.pk)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0025_saas_company_membership"),
    ]

    operations = [
        migrations.AlterField(
            model_name="company",
            name="owner",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="owned_companies", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="category",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="product",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="pricehistory",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="customer",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="shift",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="sale",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="saleitem",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="salereturn",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="salereturnitem",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="purchase",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="purchaseitem",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="stockadjustment",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="stockmovement",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="cashoperation",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="bankaccount",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="doctemplate",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="printtemplate",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="supplieritem",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="priceimport",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="crossref",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="markuptier",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="supplierapi",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="partoe",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="partinfo",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="crossreject",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="customercar",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="receipt",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="pricerule",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="supplierpricechange",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="payplan",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="shipment",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="serviceappointment",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="serviceorder",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="serviceorderline",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.AddField(
            model_name="servicepayment",
            name="company",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.company"),
        ),
        migrations.RunPython(assign_legacy_company, migrations.RunPython.noop),
    ]
