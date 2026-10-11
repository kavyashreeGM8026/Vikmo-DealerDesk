import decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Dealer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("dealer_code", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("city", models.CharField(blank=True, default="", max_length=128)),
                ("phone", models.CharField(blank=True, default="", max_length=32)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("gstin", models.CharField(blank=True, default="", max_length=32)),
                (
                    "credit_limit",
                    models.DecimalField(
                        decimal_places=2, default=decimal.Decimal("0.00"), max_digits=12
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
    ]
