"""Work out `cited_skus` for an assistant reply (API contract section 9).

`cited_skus` lists every SKU the reply mentions or recommends, and each must exist.

A SKU is cited only if ALL of these hold:
  1. it came from a trusted tool result (search_parts, check_stock when found, or
     create_draft_order) of this turn or an earlier turn of the same conversation;
  2. the reply text actually mentions it (whole-token match, case-insensitive);
  3. it exists in the database.

SKU-looking text in the reply that no tool vouched for is ignored. The result keeps the
order of first mention in the reply and has no duplicates. Malformed tool output never
raises: it is skipped.
"""
import re

from core.models import Product


def _get(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_dicts(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _sku(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _skus_from_result(name, result):
    """SKUs vouched for by one tool result. Only the three known tools count."""
    if not isinstance(result, dict):
        return []
    if name == "search_parts":
        return [_sku(p.get("sku")) for p in _as_dicts(result.get("products"))]
    if name == "check_stock":
        return [_sku(result.get("sku"))] if result.get("found") is True else []
    if name == "create_draft_order":
        order = result.get("order")
        return [_sku(i.get("sku")) for i in _as_dicts(order.get("items"))] if isinstance(order, dict) else []
    return []


def _trusted_skus(tool_results, history):
    trusted = set()

    for entry in tool_results if isinstance(tool_results, (list, tuple)) else []:
        if isinstance(entry, dict):
            trusted.update(_skus_from_result(entry.get("name"), entry.get("result")))

    # Earlier turns: the Gemini history keeps each tool result as a function_response part.
    for content in history if isinstance(history, (list, tuple)) else []:
        parts = _get(content, "parts")
        for part in parts if isinstance(parts, (list, tuple)) else []:
            function_response = _get(part, "function_response")
            if function_response is None:
                continue
            response = _get(function_response, "response")
            if isinstance(response, dict):
                trusted.update(_skus_from_result(_get(function_response, "name"), response.get("result")))

    trusted.discard(None)
    return trusted


def _mention_pattern(sku):
    # Not part of a longer code: no letter/digit/underscore (or "<alnum>-") right before,
    # and no letter/digit/underscore (or "-<alnum>") right after.
    return re.compile(
        r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9]-)" + re.escape(sku) + r"(?![A-Za-z0-9_])(?!-[A-Za-z0-9])",
        re.IGNORECASE,
    )


def extract_cited_skus(reply, tool_results, history=None):
    """Return the SKUs that `reply` mentions, backed by trusted tool results and the database."""
    if not isinstance(reply, str) or not reply:
        return []

    first_mention = {}
    for sku in _trusted_skus(tool_results, history):
        match = _mention_pattern(sku).search(reply)
        if match:
            first_mention[sku] = match.start()
    if not first_mention:
        return []

    existing = set(Product.objects.filter(sku__in=list(first_mention)).values_list("sku", flat=True))
    return sorted((sku for sku in first_mention if sku in existing), key=lambda sku: (first_mention[sku], sku))
