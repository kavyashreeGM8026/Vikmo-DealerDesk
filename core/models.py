from decimal import Decimal

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
