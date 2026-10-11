from decimal import Decimal

from rest_framework import serializers

from common.fields import StrictIntegerField
from core.models import Dealer, Product


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


class NonNegativeMoneyField(MoneyField):
    """Money that may be 0 (e.g. a credit limit). Same 2-decimal string format as MoneyField."""

    def to_internal_value(self, data):
        # Skip MoneyField's "must be > 0" rule by calling DecimalField directly.
        # DecimalField already rejects more than 2 decimal places and non-numbers.
        value = serializers.DecimalField.to_internal_value(self, data)
        if value < 0:
            raise serializers.ValidationError("Credit limit must not be negative.")
        return value


class DealerSerializer(serializers.ModelSerializer):
    """Dealer in and out (contract section 6).

    Required: dealer_code, name. Optional: city, phone, email, gstin, credit_limit.
    `null` for an optional field is treated as "not given".
    """

    credit_limit = NonNegativeMoneyField(required=False, allow_null=True)

    class Meta:
        model = Dealer
        fields = (
            "id",
            "dealer_code",
            "name",
            "city",
            "phone",
            "email",
            "gstin",
            "credit_limit",
        )
        extra_kwargs = {
            # Uniqueness is a 409 DUPLICATE in the view, not a 400 validation error.
            "dealer_code": {"validators": []},
            "city": {"allow_null": True},
            "phone": {"allow_null": True},
            "email": {"allow_null": True},
            "gstin": {"allow_null": True},
        }

    def validate(self, attrs):
        for key in ("city", "phone", "email", "gstin"):
            if key in attrs and attrs[key] is None:
                attrs[key] = ""
        if "credit_limit" in attrs and attrs["credit_limit"] is None:
            del attrs["credit_limit"]  # fall back to the model default (0.00)
        return attrs
