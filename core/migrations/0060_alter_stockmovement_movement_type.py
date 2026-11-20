from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0059_supplier_replacement_receipts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stockmovement",
            name="movement_type",
            field=models.CharField(
                choices=[
                    ("purchase", "Надходження"),
                    ("supplier_replacement", "Заміна від постачальника"),
                    ("sale", "Видаток продажу"),
                    ("customer_return", "Повернення від клієнта"),
                    ("supplier_return", "Повернення постачальнику"),
                    ("writeoff", "Списання"),
                    ("surplus", "Надлишок"),
                    ("shortage", "Нестача"),
                    ("opening", "Початковий залишок"),
                    ("transit_stock", "Оприбутковано: клієнт не забрав"),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
    ]
