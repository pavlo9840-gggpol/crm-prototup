from django.db import migrations, models
from django.db.models import Q


def assign_legacy_rows_to_single_company(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    companies = list(Company.objects.all().values_list("pk", flat=True)[:2])
    if len(companies) != 1:
        return
    company_id = companies[0]
    for model_name in ("PartOe", "PartInfo", "CrossRef", "CrossReject"):
        apps.get_model("core", model_name).objects.filter(company__isnull=True).update(company_id=company_id)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0063_financeperiodclose"),
    ]

    operations = [
        migrations.RunPython(assign_legacy_rows_to_single_company, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(name="partoe", unique_together=set()),
        migrations.AlterUniqueTogether(name="partinfo", unique_together=set()),
        migrations.AlterUniqueTogether(name="crossref", unique_together=set()),
        migrations.AlterUniqueTogether(name="crossreject", unique_together=set()),
        migrations.AddConstraint(
            model_name="partoe",
            constraint=models.UniqueConstraint(fields=("brand", "sku_norm", "oe_norm"), condition=Q(company__isnull=True), name="uniq_partoe_global"),
        ),
        migrations.AddConstraint(
            model_name="partoe",
            constraint=models.UniqueConstraint(fields=("company", "brand", "sku_norm", "oe_norm"), condition=Q(company__isnull=False), name="uniq_partoe_company"),
        ),
        migrations.AddConstraint(
            model_name="partinfo",
            constraint=models.UniqueConstraint(fields=("brand", "sku_norm"), condition=Q(company__isnull=True), name="uniq_partinfo_global"),
        ),
        migrations.AddConstraint(
            model_name="partinfo",
            constraint=models.UniqueConstraint(fields=("company", "brand", "sku_norm"), condition=Q(company__isnull=False), name="uniq_partinfo_company"),
        ),
        migrations.AddConstraint(
            model_name="crossref",
            constraint=models.UniqueConstraint(fields=("a", "b"), condition=Q(company__isnull=True), name="uniq_crossref_global"),
        ),
        migrations.AddConstraint(
            model_name="crossref",
            constraint=models.UniqueConstraint(fields=("company", "a", "b"), condition=Q(company__isnull=False), name="uniq_crossref_company"),
        ),
        migrations.AddConstraint(
            model_name="crossreject",
            constraint=models.UniqueConstraint(fields=("a", "b"), condition=Q(company__isnull=True), name="uniq_crossreject_global"),
        ),
        migrations.AddConstraint(
            model_name="crossreject",
            constraint=models.UniqueConstraint(fields=("company", "a", "b"), condition=Q(company__isnull=False), name="uniq_crossreject_company"),
        ),
    ]
