"""Order pricing: the single place where money and GST are calculated (contract section 7.4).

Only `decimal.Decimal` is used for money. Floats are rejected outright, so a float can never
sneak into a calculation.

    line_subtotal = unit_price x quantity
    line_tax      = line_subtotal x 0.18, rounded to 2 places, ROUND_HALF_UP, PER LINE
    line_total    = line_subtotal + line_tax
    subtotal / tax_total / grand_total are the sums of the lines (tax is not re-rounded).
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext   

TAX_RATE = Decimal("0.18")
PAISE = Decimal("0.01")


@dataclass(frozen=True)
class LinePricing:
    unit_price: Decimal
    quantity: int
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class OrderPricing:
    lines: tuple
    subtotal: Decimal
    tax_total: Decimal
    grand_total: Decimal


def to_paise(value: Decimal) -> Decimal:
    """Round to 2 decimal places, halves rounding up (0.005 -> 0.01)."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def price_line(unit_price, quantity) -> LinePricing:
    if not isinstance(unit_price, Decimal):
        raise TypeError("unit_price must be a Decimal, never a float or int.")
    if not unit_price.is_finite() or unit_price < 0:
        raise ValueError("unit_price must be a finite amount that is not negative.")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise TypeError("quantity must be an integer.")
    if quantity < 1:
        raise ValueError("quantity must be at least 1.")

    with localcontext() as ctx:
        ctx.prec = 50  # far more digits than any amount we store, so nothing is silently trimmed
        line_subtotal = to_paise(unit_price * quantity)
        line_tax = to_paise(line_subtotal * TAX_RATE)
        line_total = line_subtotal + line_tax
    return LinePricing(to_paise(unit_price), quantity, line_subtotal, line_tax, line_total)


def price_order(lines) -> OrderPricing:
    """`lines` is an iterable of (unit_price: Decimal, quantity: int) pairs."""
    priced = tuple(price_line(unit_price, quantity) for unit_price, quantity in lines)
    subtotal = sum((p.line_subtotal for p in priced), Decimal("0.00"))
    tax_total = sum((p.line_tax for p in priced), Decimal("0.00"))
    return OrderPricing(priced, subtotal, tax_total, subtotal + tax_total)
