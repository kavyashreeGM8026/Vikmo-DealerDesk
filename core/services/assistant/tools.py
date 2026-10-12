"""Database-backed tools for the Dealer Assistant (contract section 9).

Three tools, with exactly these names: `search_parts`, `check_stock`, `create_draft_order`.

* Every tool reads or writes live PostgreSQL data. Nothing here calls an LLM.
* Every argument is validated server-side. The model's arguments are never trusted: bad input
  comes back as a plain `{"ok": False, "error": ...}` result, it never raises.
* Catalogue text (names, descriptions) is untrusted data. It is cleaned and truncated, and
  descriptions are returned under the key `description_untrusted` so they cannot be mistaken
  for instructions.
* `create_draft_order` reuses the existing OrderCreateSerializer and create_draft_order service
  (so validation, GST pricing and order numbers are the same as POST /api/orders/). It can only
  create DRAFT orders. The dealer always comes from the request, never from the model.
"""
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q

from common.exceptions import APIError, flatten_errors
from core.models import Dealer, Product
from core.serializers import OrderCreateSerializer, OrderOutputSerializer
from core.services.orders import create_draft_order as create_draft_order_service
from core.services.orders import present_order

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 5
MAX_ARG_LENGTH = 100
MAX_SEARCH_TERMS = 8
MAX_ORDER_LINES = 20
MAX_TEXT = {"name": 150, "description": 200, "other": 100}
MAX_DRAFTS_PER_REQUEST = 1

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TERM = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
_STOPWORDS = {
    "a", "an", "the", "for", "of", "and", "or", "with", "in", "on", "to", "my", "me", "do", "does",
    "you", "your", "have", "has", "any", "some", "is", "are", "got", "need", "want", "show", "find",
    "please", "pls", "i", "we", "it", "this", "that", "fit", "fits", "suitable", "available",
}


@dataclass
class ToolContext:
    """Per-request state shared by the tools. Built by the view; the model cannot change it."""

    dealer: Dealer
    created_orders: list = field(default_factory=list)  # order numbers created in this request


# ---------------------------------------------------------------------------- helpers
def clean_text(value, limit):
    """Untrusted catalogue text -> one short line without control characters."""
    text = " ".join(_CONTROL_CHARS.sub(" ", str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _product_view(product, with_description=True):
    data = {
        "sku": product.sku,
        "name": clean_text(product.name, MAX_TEXT["name"]),
        "brand": clean_text(product.brand, MAX_TEXT["other"]),
        "category": clean_text(product.category, MAX_TEXT["other"]),
        "vehicle_fitment": clean_text(product.vehicle_fitment, MAX_TEXT["other"]),
        "price": _money(product.price),
        "stock": product.stock,
    }
    if with_description:
        data["description_untrusted"] = clean_text(product.description, MAX_TEXT["description"])
    return data


def _error(message, details=None):
    result = {"ok": False, "error": message}
    if details:
        result["details"] = details
    return result


def _text_arg(args, name, required=False):
    """Return a cleaned string argument, None if absent, or raise ValueError with a message."""
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"'{name}' is required.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{name}' must be a string.")
    value = " ".join(_CONTROL_CHARS.sub(" ", value).split())
    if len(value) > MAX_ARG_LENGTH:
        raise ValueError(f"'{name}' is too long (max {MAX_ARG_LENGTH} characters).")
    return value


def _terms(text, stem):
    """Lower-case search words. `stem` drops a plural 's' so 'pads' matches 'Pad'."""
    terms = []
    for word in _TERM.findall(text.lower()):
        if stem and word in _STOPWORDS:
            continue
        if stem and len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word not in terms:
            terms.append(word)
    return terms[:MAX_SEARCH_TERMS]


# ---------------------------------------------------------------------------- search_parts
def search_parts(args, ctx):
    """Find products by words in SKU, name, brand, category or vehicle fitment.

    Simple text matching in PostgreSQL (case-insensitive substring; every search word must
    appear in at least one of those fields). No embeddings or vector search.
    """
    try:
        query = _text_arg(args, "query")
        vehicle = _text_arg(args, "vehicle")
        category = _text_arg(args, "category")
    except ValueError as exc:
        return _error(str(exc))

    query_terms = _terms(query, stem=True) if query else []
    vehicle_terms = _terms(vehicle, stem=False) if vehicle else []
    if vehicle and not vehicle_terms:
        # A vehicle filter was given but has nothing searchable in it (e.g. "%"). No vehicle can
        # match it, so the honest answer is an empty result. Ignoring the filter would wrongly
        # widen the search. (A query with no searchable words is still an error, below.)
        return {"ok": True, "count": 0, "more_results_exist": False, "products": []}
    if not (query_terms or vehicle_terms or category):
        return _error("Give at least one of: query, vehicle, category.")

    qs = Product.objects.all()
    for term in query_terms:
        qs = qs.filter(
            Q(sku__icontains=term)
            | Q(name__icontains=term)
            | Q(brand__icontains=term)
            | Q(category__icontains=term)
            | Q(vehicle_fitment__icontains=term)
        )
    for term in vehicle_terms:
        qs = qs.filter(vehicle_fitment__icontains=term)
    if category:
        qs = qs.filter(category__icontains=category)

    found = list(qs.order_by("sku")[: SEARCH_LIMIT + 1])
    return {
        "ok": True,
        "count": min(len(found), SEARCH_LIMIT),
        "more_results_exist": len(found) > SEARCH_LIMIT,
        "products": [_product_view(p) for p in found[:SEARCH_LIMIT]],
    }


# ---------------------------------------------------------------------------- check_stock
def check_stock(args, ctx):
    """Exact SKU lookup. An unknown SKU (`found: False`) is different from stock 0."""
    try:
        sku = _text_arg(args, "sku", required=True)
    except ValueError as exc:
        return _error(str(exc))

    product = Product.objects.filter(pk=sku).first()
    if product is None:
        matches = list(Product.objects.filter(sku__iexact=sku)[:2])
        product = matches[0] if len(matches) == 1 else None
    if product is None:
        return {"ok": True, "found": False, "sku": sku, "message": "No product with this SKU exists."}

    view = _product_view(product)
    view.update({"ok": True, "found": True, "in_stock": product.stock > 0})
    return view


# ---------------------------------------------------------------------------- create_draft_order
def _normalise_items(items):
    """Tidy the model's order lines before the strict serializer sees them.

    * SKU text loses control characters (PostgreSQL cannot store NUL).
    * Some SDK paths hand back JSON numbers as floats (3.0): a whole-number float becomes an
      int. Everything else (2.5, "3", True) still goes to the strict serializer and is rejected.
    """
    fixed = []
    for item in items:
        if isinstance(item, dict):
            item = dict(item)
            if isinstance(item.get("sku"), str):
                item["sku"] = " ".join(_CONTROL_CHARS.sub(" ", item["sku"]).split())
            quantity = item.get("quantity")
            if isinstance(quantity, float) and quantity.is_integer():
                item["quantity"] = int(quantity)
        fixed.append(item)
    return fixed


def create_draft_order(args, ctx):
    """Create a DRAFT order for the current dealer from explicit SKUs and quantities."""
    if len(ctx.created_orders) >= MAX_DRAFTS_PER_REQUEST:
        return _error("A draft order was already created in this request. Do not create another.")

    items = args.get("items")
    if not isinstance(items, list) or not items:
        return _error("'items' must be a non-empty list of {sku, quantity}.")
    if len(items) > MAX_ORDER_LINES:
        return _error(f"An order can have at most {MAX_ORDER_LINES} lines.")

    # Only `items` is read from the model's arguments: the dealer is fixed by the request.
    serializer = OrderCreateSerializer(
        data={"dealer_id": ctx.dealer.id, "items": _normalise_items(items)}
    )
    if not serializer.is_valid():
        details = flatten_errors(serializer.errors)
        return _error("The order was not created because the items are invalid.", details)

    try:
        order = create_draft_order_service(
            serializer.validated_data["dealer"], serializer.validated_data["lines"]
        )
    except APIError as exc:
        return _error(exc.message)

    data = OrderOutputSerializer(present_order(order)).data
    ctx.created_orders.append(data["order_number"])
    return {
        "ok": True,
        "message": "Draft order created. It is not confirmed and no stock is reserved.",
        "order": {
            "order_number": data["order_number"],
            "status": data["status"],
            "items": [
                {k: row[k] for k in ("sku", "name", "quantity", "unit_price", "line_total")}
                for row in data["items"]
            ],
            "subtotal": data["subtotal"],
            "tax_total": data["tax_total"],
            "grand_total": data["grand_total"],
        },
    }


# ---------------------------------------------------------------------------- registry
TOOLS = {
    "search_parts": search_parts,
    "check_stock": check_stock,
    "create_draft_order": create_draft_order,
}

# Plain JSON-schema descriptions. llm.py converts these to Gemini function declarations, so this
# module does not need the Gemini SDK.
TOOL_SPECS = [
    {
        "name": "search_parts",
        "description": (
            "Search the live product catalogue. Matches words in SKU, name, brand, category and "
            "vehicle fitment. Returns at most 5 products with SKU, name, fitment, current price "
            "(INR) and current stock."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the dealer wants, e.g. 'brake pads'."},
                "vehicle": {"type": "string", "description": "Vehicle make and model, e.g. 'Bajaj Pulsar 150'."},
                "category": {"type": "string", "description": "Product category, e.g. 'Brakes'."},
            },
        },
    },
    {
        "name": "check_stock",
        "description": (
            "Look up one product by its exact SKU and return its current stock and price. "
            "Says clearly if the SKU does not exist (different from stock 0)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "Exact SKU, e.g. 'BRK-1044'."}},
            "required": ["sku"],
        },
    },
    {
        "name": "create_draft_order",
        "description": (
            "Create a DRAFT order for the current dealer. Only call this when the dealer has "
            "clearly asked to order and has given an exact SKU and a whole-number quantity for "
            "every line. Never confirms an order."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Order lines. Each SKU may appear only once.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "quantity": {"type": "integer", "description": "Whole number, at least 1."},
                        },
                        "required": ["sku", "quantity"],
                    },
                }
            },
            "required": ["items"],
        },
    },
]


def execute_tool(name, args, ctx):
    """Run one tool by name. Always returns a dict; never raises."""
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        return _error(f"Unknown tool '{clean_text(name, 50)}'.")
    if not isinstance(args, dict):
        return _error("Tool arguments must be an object.")
    try:
        return tool(args, ctx)
    except Exception:  # noqa: BLE001 - keep internals away from the model and the client
        logger.exception("Assistant tool %s failed", name)
        return _error("The tool failed. Try again later.")
