"""Order business logic for Stage 2d-1: create a DRAFT, load orders, and build the response view.

Lifecycle actions (confirm, cancel, ...) are added in Stage 2d-2.
"""
import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch  

from common.exceptions import APIError, NotFoundError
from core.models import Order, OrderItem
from core.services.order_numbers import next_order_number
from core.services.pricing import price_order

# numeric(14,2) holds up to 999,999,999,999.99
MAX_ORDER_TOTAL = Decimal("999999999999.99")
_ORDER_NUMBER = re.compile(r"^ORD-\d{8}-\d{4,}$")


def order_queryset():
    """Orders with their items (and each item's product) loaded in 2 extra queries in total."""
    items = OrderItem.objects.select_related("product").order_by("id")
    return Order.objects.prefetch_related(Prefetch("items", queryset=items))


def get_order(order_number: str) -> Order:
    # Reject anything that cannot be an order number before touching the database.
    if not _ORDER_NUMBER.match(order_number):
        raise NotFoundError(f"Order '{order_number}' not found.")
    try:
        return order_queryset().get(order_number=order_number)
    except Order.DoesNotExist:
        raise NotFoundError(f"Order '{order_number}' not found.")


def create_draft_order(dealer, lines) -> Order:
    """Create a DRAFT. `lines` is a list of (Product, quantity), already validated.

    Everything that can fail is checked BEFORE the first write. Stock is never touched.
    """
    pricing = price_order((product.price, quantity) for product, quantity in lines)
    if pricing.grand_total > MAX_ORDER_TOTAL:
        raise APIError(
            "Order total is too large.",
            [{"field": "items", "message": "Order total is too large."}],
        )

    with transaction.atomic():
        # Allocate the number last: its row lock is held until this transaction commits.
        order = Order.objects.create(
            order_number=next_order_number(),
            dealer=dealer,
            status=Order.Status.DRAFT,
            subtotal=pricing.subtotal,
            tax_total=pricing.tax_total,
            grand_total=pricing.grand_total,
        )
        OrderItem.objects.bulk_create(
            [
                OrderItem(
                    order=order,
                    product=product,
                    product_name=product.name,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_subtotal=line.line_subtotal,
                    line_tax=line.line_tax,
                    line_total=line.line_total,
                )
                for (product, _), line in zip(lines, pricing.lines)
            ]
        )
    return get_order(order.order_number)


def present_order(order: Order) -> dict:
    """Build the response data for an order. Read-only: nothing is written to the database.

    * DRAFT: priced from the CURRENT catalogue (price and name), so the response always shows
      what the order would cost right now.
    * Any other status: the stored (locked) prices, totals and name snapshot.
    """
    items = list(order.items.all())  # prefetched, ordered by id
    if order.status == Order.Status.DRAFT:
        pricing = price_order((item.product.price, item.quantity) for item in items)
        rows = [
            {
                "sku": item.product_id,
                "name": item.product.name,
                "quantity": item.quantity,
                "unit_price": line.unit_price,
                "line_subtotal": line.line_subtotal,
                "line_tax": line.line_tax,
                "line_total": line.line_total,
            }
            for item, line in zip(items, pricing.lines)
        ]
        subtotal, tax_total, grand_total = pricing.subtotal, pricing.tax_total, pricing.grand_total
    else:
        rows = [
            {
                "sku": item.product_id,
                "name": item.product_name,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "line_subtotal": item.line_subtotal,
                "line_tax": item.line_tax,
                "line_total": item.line_total,
            }
            for item in items
        ]
        subtotal, tax_total, grand_total = order.subtotal, order.tax_total, order.grand_total

    return {
        "order_number": order.order_number,
        "dealer_id": order.dealer_id,
        "status": order.status,
        "items": rows,
        "subtotal": subtotal,
        "tax_total": tax_total,
        "grand_total": grand_total,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }
