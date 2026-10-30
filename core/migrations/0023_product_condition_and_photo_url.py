# Generated for product condition and external image support.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0022_service_payment")]

    operations = [
        migrations.AddField(model_name="product", name="photo_url", field=models.URLField(blank=True, max_length=500, verbose_name="Фото за посиланням")),
        migrations.AddField(model_name="product", name="condition", field=models.CharField(choices=[("new", "Новий"), ("used", "Б/В"), ("refurbished", "Відновлений"), ("incomplete", "Некомплект")], db_index=True, default="new", max_length=15, verbose_name="Стан товару")),
        migrations.AddField(model_name="product", name="has_defect", field=models.BooleanField(default=False, verbose_name="Є дефект")),
        migrations.AddField(model_name="product", name="condition_description", field=models.TextField(blank=True, verbose_name="Опис стану")),
    ]
