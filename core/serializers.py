from decimal import Decimal

from rest_framework import serializers

from common.fields import StrictIntegerField
from core.models import Product


def validate_positive_money(value: Decimal):
    """Price must be > 0 and have at most 2 decimal places (contract §4)."""
    if value <= 0:
        raise serializers.ValidationError("Price must be greater than 0.")
    exponent = value.as_tuple().exponent
    if exponent < -2:
        raise serializers.ValidationError("Price must have at most 2 decimal places.")
    return value


class MoneyField(serializers.DecimalField):
    """JSON money: a string with exactly 2 decimal places, e.g. \"1459.50\"."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("coerce_to_string", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        return validate_positive_money(value)


class ProductSerializer(serializers.ModelSerializer):
    price = MoneyField()
    stock = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = (
            "sku",
            "name",
            "category",
            "brand",
            "vehicle_fitment",
            "price",
            "stock",
            "description",
        )


class ProductCreateSerializer(serializers.ModelSerializer):
    """POST body: catalogue fields except stock. New products start at stock 0."""

    price = MoneyField()
    description = serializers.CharField(allow_blank=True, required=False, default="")

    class Meta:
        model = Product
        fields = (
            "sku",
            "name",
            "category",
            "brand",
            "vehicle_fitment",
            "price",
            "description",
        )
        # Uniqueness is a 409 DUPLICATE in the view, not a 400 validation error.
        extra_kwargs = {"sku": {"validators": []}}

    def create(self, validated_data):
        return Product.objects.create(stock=0, **validated_data)


class ProductUpdateSerializer(serializers.ModelSerializer):
    """PATCH: any catalogue field except sku. Stock is changed via inventory."""

    sku = serializers.CharField(read_only=True)
    price = MoneyField(required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    stock = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = (
            "sku",
            "name",
            "category",
            "brand",
            "vehicle_fitment",
            "price",
            "stock",
            "description",
        )


class InventoryUpdateSerializer(serializers.Serializer):
    quantity = StrictIntegerField(min_value=0)
    reason = serializers.CharField(required=False, allow_blank=True)
