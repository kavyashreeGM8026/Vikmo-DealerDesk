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


# ---- Orders (contract section 7) ---------------------------------------------------------

MAX_QUANTITY = 2_147_483_647  # largest value the integer column can hold
MAX_ID = 2**63 - 1  # largest value the bigint id column can hold


class OrderItemInputSerializer(serializers.Serializer):
    sku = serializers.CharField()
    quantity = StrictIntegerField(min_value=1, max_value=MAX_QUANTITY)


class OrderCreateSerializer(serializers.Serializer):
    """POST /api/orders/ body. Validation only reads the database; nothing is written.

    On success `validated_data` is {"dealer": Dealer, "lines": [(Product, quantity), ...]}.
    """

    dealer_id = StrictIntegerField(min_value=1, max_value=MAX_ID)
    items = OrderItemInputSerializer(many=True, allow_empty=False)

    def validate(self, attrs):
        errors = {}

        skus = [item["sku"] for item in attrs["items"]]
        duplicates = sorted({sku for sku in skus if skus.count(sku) > 1})
        item_messages = []
        if duplicates:
            item_messages.append(f"Duplicate SKU in order: {', '.join(duplicates)}.")

        products = Product.objects.in_bulk(set(skus))
        unknown = sorted({sku for sku in skus if sku not in products})
        if unknown:
            item_messages.append(f"Unknown SKU: {', '.join(unknown)}.")
        if item_messages:
            errors["items"] = item_messages

        dealer = Dealer.objects.filter(pk=attrs["dealer_id"]).first()
        if dealer is None:
            errors["dealer_id"] = [f"Dealer {attrs['dealer_id']} does not exist."]

        if errors:
            raise serializers.ValidationError(errors)

        lines = [(products[item["sku"]], item["quantity"]) for item in attrs["items"]]
        return {"dealer": dealer, "lines": lines}


class OrderItemOutputSerializer(serializers.Serializer):
    sku = serializers.CharField()
    name = serializers.CharField()
    quantity = serializers.IntegerField()
    unit_price = MoneyField(max_digits=16)
    line_subtotal = MoneyField(max_digits=16)
    line_tax = MoneyField(max_digits=16)
    line_total = MoneyField(max_digits=16)


class OrderOutputSerializer(serializers.Serializer):
    """Formats the dict built by core.services.orders.present_order."""

    order_number = serializers.CharField()
    dealer_id = serializers.IntegerField()
    status = serializers.CharField()
    items = OrderItemOutputSerializer(many=True)
    subtotal = MoneyField(max_digits=16)
    tax_total = MoneyField(max_digits=16)
    grand_total = MoneyField(max_digits=16)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

