# HAND-WRITTEN migration (no Django available when it was authored).
# Verify with:  python manage.py makemigrations --check --dry-run
# If that reports changes, delete this file and run:  python manage.py makemigrations core
import decimal

import django
import django.db.models.deletion
from django.db import migrations, models


def _check(condition):
    """`check=` on Django < 5.1, `condition=` on 5.1+ (same as core/models.py)."""
    return {"condition": condition} if django.VERSION >= (5, 1) else {"check": condition}


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_dealer"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderNumberSequence",
            fields=[
                ("day", models.DateField(primary_key=True, serialize=False)),
                ("last_value", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="Order",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("order_number", models.CharField(max_length=32, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("CONFIRMED", "Confirmed"),
                            ("DISPATCHED", "Dispatched"),
                            ("DELIVERED", "Delivered"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="DRAFT",
                        max_length=16,
                    ),
                ),
                (
                    "subtotal",
                    models.DecimalField(
                        decimal_places=2, default=decimal.Decimal("0.00"), max_digits=14
                    ),
                ),
                (
                    "tax_total",
                    models.DecimalField(
                        decimal_places=2, default=decimal.Decimal("0.00"), max_digits=14
                    ),
                ),
                (
                    "grand_total",
                    models.DecimalField(
                        decimal_places=2, default=decimal.Decimal("0.00"), max_digits=14
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "dealer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="core.dealer",
                    ),
                ),
            ],
            options={
                "ordering": ["-id"],
            },
        ),
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("product_name", models.CharField(max_length=255)),
                ("quantity", models.PositiveIntegerField()),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=12)),
                ("line_subtotal", models.DecimalField(decimal_places=2, max_digits=14)),
                ("line_tax", models.DecimalField(decimal_places=2, max_digits=14)),
                ("line_total", models.DecimalField(decimal_places=2, max_digits=14)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="core.order",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_items",
                        to="core.product",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                name="order_status_valid",
                **_check(
                    models.Q(
                        status__in=["DRAFT", "CONFIRMED", "DISPATCHED", "DELIVERED", "CANCELLED"]
                    )
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.UniqueConstraint(
                fields=["order", "product"], name="orderitem_unique_product_per_order"
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                name="orderitem_quantity_gte_1",
                **_check(models.Q(quantity__gte=1)),
            ),
        ),
    ]
