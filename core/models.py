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
