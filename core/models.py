from decimal import Decimal

import django
from django.db import models


class Product(models.Model):
    """One catalogue item. `sku` is the public identifier and primary key."""

    sku = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=128)
    brand = models.CharField(max_length=128)
    vehicle_fitment = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return self.sku


class Dealer(models.Model):
    """A dealer who places orders. `dealer_code` is the public business identifier (unique).

    Only `dealer_code` and `name` are required by the API contract (section 6); the rest are
    optional and default to an empty string (credit_limit defaults to 0.00).
    """

    dealer_code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=128, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(max_length=254, blank=True, default="")
    gstin = models.CharField(max_length=32, blank=True, default="")
    # DecimalField (never float) so money is exact to the paisa.
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.dealer_code


def _check_constraint(name, condition):
    """CheckConstraint spelled for both Django 4.2-5.0 (`check=`) and 5.1+ (`condition=`)."""
    if django.VERSION >= (5, 1):
        return models.CheckConstraint(name=name, condition=condition)
    return models.CheckConstraint(name=name, check=condition)


class Order(models.Model):
    """A dealer's order. Created as DRAFT; the lifecycle actions arrive in Stage 2d-2.

    For a DRAFT the stored money fields are only a snapshot taken at creation: API responses
    re-price drafts from the live catalogue. They become authoritative once confirmed.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        CONFIRMED = "CONFIRMED", "Confirmed"
        DISPATCHED = "DISPATCHED", "Dispatched"
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"

    order_number = models.CharField(max_length=32, unique=True)
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    grand_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            _check_constraint(
                "order_status_valid",
                models.Q(status__in=["DRAFT", "CONFIRMED", "DISPATCHED", "DELIVERED", "CANCELLED"]),
            ),
        ]

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    """One line of an order. A product can appear only once per order (DB constraint)."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items")
    # Snapshot of the product name so historical orders stay readable if the catalogue changes.
    product_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    line_tax = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"], name="orderitem_unique_product_per_order"
            ),
            _check_constraint("orderitem_quantity_gte_1", models.Q(quantity__gte=1)),
        ]

    def __str__(self):
        return f"{self.order_id}:{self.product_id} x{self.quantity}"


class OrderNumberSequence(models.Model):
    """One counter row per calendar day (Asia/Kolkata). See core/services/order_numbers.py."""

    day = models.DateField(primary_key=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.day}: {self.last_value}"
