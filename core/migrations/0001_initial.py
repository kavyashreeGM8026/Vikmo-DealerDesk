from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Product",
            fields=[
                ("sku", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=255)),
                ("category", models.CharField(max_length=128)),
                ("brand", models.CharField(max_length=128)),
                ("vehicle_fitment", models.CharField(max_length=255)),
                ("price", models.DecimalField(decimal_places=2, max_digits=12)),
                ("stock", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["sku"],
            },
        ),
    ]
