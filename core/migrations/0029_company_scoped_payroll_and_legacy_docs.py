import django.conf
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(django.conf.settings.AUTH_USER_MODEL),
        ("core", "0028_company_scoped_identifiers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="doctemplate",
            name="kind",
            field=models.CharField(max_length=20),
        ),
        migrations.AlterField(
            model_name="payplan",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pay_plans",
                to=django.conf.settings.AUTH_USER_MODEL,
                verbose_name="Менеджер (порожньо = типовий план для всіх)",
            ),
        ),
        migrations.AddConstraint(
            model_name="doctemplate",
            constraint=models.UniqueConstraint(
                condition=Q(company__isnull=False),
                fields=("company", "kind"),
                name="uniq_company_legacy_doc_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="payplan",
            constraint=models.UniqueConstraint(
                condition=Q(company__isnull=False, user__isnull=False),
                fields=("company", "user"),
                name="uniq_company_payplan_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="payplan",
            constraint=models.UniqueConstraint(
                condition=Q(company__isnull=False, user__isnull=True),
                fields=("company",),
                name="uniq_company_payplan_default",
            ),
        ),
    ]
