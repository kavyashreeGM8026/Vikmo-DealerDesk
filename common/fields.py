from rest_framework import serializers


class StrictIntegerField(serializers.Field):
    """Accepts only a real JSON integer.

    DRF's built-in IntegerField quietly accepts "25" and 25.0, which would hide client
    mistakes. The contract says quantities must be integers, so we reject true/false,
    2.5, 25.0 and "25" with a 400.
    """

    default_error_messages = {
        "invalid": "A valid integer is required.",
        "min_value": "Ensure this value is greater than or equal to {min_value}.",
        "max_value": "Ensure this value is less than or equal to {max_value}.",
    }

    def __init__(self, min_value=None, max_value=None, **kwargs):
        self.min_value = min_value
        self.max_value = max_value
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        # bool is a subclass of int in Python, so it must be rejected explicitly
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail("invalid")
        if self.min_value is not None and data < self.min_value:
            self.fail("min_value", min_value=self.min_value)
        if self.max_value is not None and data > self.max_value:
            self.fail("max_value", max_value=self.max_value)
        return data

    def to_representation(self, value):
        return int(value)
