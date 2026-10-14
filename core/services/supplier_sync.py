
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction

from core.models import Product


MAX_ATTEMPTS = 5
PAGE_SIZE = 20
MAX_PRICE = Decimal("9999999999.99")

# Longest value each text column can hold (core/models.py). A longer value would make the
# database raise and fail the whole sync, so such an item is rejected instead.
MAX_SKU_LENGTH = 64
TEXT_FIELDS = {
    "name": 255,
    "category": 128,
    "brand": 128,
    "vehicle_fitment": 255,
    "description": None,  # TextField: no limit
}


class SupplierSyncError(Exception):
    """Raised when the supplier feed cannot be fetched safely."""


def _fetch_page(cursor, stats):
    """Fetch one supplier page, retrying temporary failures."""
    url = f"{settings.SUPPLIER_API_URL.rstrip('/')}/v1/products"
    headers = {"X-Api-Key": settings.SUPPLIER_API_KEY}
    params = {"limit": PAGE_SIZE}

    if cursor:
        params["cursor"] = cursor

    for attempt in range(MAX_ATTEMPTS):
        stats["requests_made"] += 1

        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=10,
            )
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise SupplierSyncError(
                    "Supplier connection failed after retries."
                ) from exc
            time.sleep(min(2 ** attempt, 8))
            continue

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise SupplierSyncError(
                    "Supplier returned invalid JSON."
                ) from exc

            if (
                not isinstance(body, dict)
                or not isinstance(body.get("items"), list)
                or ("next_cursor" not in body)
            ):
                raise SupplierSyncError(
                    "Supplier returned an invalid page."
                )

            return body

        if response.status_code == 401:
            raise SupplierSyncError("Supplier API key was rejected.")

        if response.status_code == 429:
            if attempt == MAX_ATTEMPTS - 1:
                raise SupplierSyncError(
                    "Supplier rate limit persisted after retries."
                )

            try:
                delay = max(0, int(response.headers.get("Retry-After", "1")))
            except ValueError:
                delay = 1

            time.sleep(min(delay, 60))
            continue

        if 500 <= response.status_code <= 599:
            if attempt == MAX_ATTEMPTS - 1:
                raise SupplierSyncError(
                    f"Supplier returned HTTP {response.status_code} after retries."
                )
            time.sleep(min(2 ** attempt, 8))
            continue

        raise SupplierSyncError(
            f"Supplier returned unexpected HTTP {response.status_code}."
        )

    raise SupplierSyncError("Supplier request failed.")


def _updated_key(value):
    """Sortable form of an `updated_at` string. Missing or unparsable counts as oldest."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return oldest
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return oldest
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_item(item):
    """Return (validated item, rejection reason).

    Per contract section 8.3 an item is invalid only when its `sku` is missing or empty or its
    `price` is not a positive amount with at most 2 decimals. Two extra guards keep one bad row
    from failing the whole sync: a value too long for its column is rejected, and a text field
    that is absent or null is simply not part of the item (it is left unchanged on an existing
    product and stored as empty on a new one).
    """
    if not isinstance(item, dict):
        return None, "invalid item"

    sku = item.get("sku")
    if not isinstance(sku, str) or not sku.strip():
        return None, "missing sku"
    if len(sku.strip()) > MAX_SKU_LENGTH:
        return None, "sku too long"

    price_value = item.get("price")
    try:
        price = Decimal(str(price_value))
    except (InvalidOperation, ValueError, TypeError):
        return None, "invalid price"

    if not price.is_finite() or price <= 0 or price > MAX_PRICE:
        return None, "invalid price"

    if price.as_tuple().exponent < -2:
        return None, "invalid price"

    valid = {
        "sku": sku.strip(),
        "price": price,
        "updated_at": item.get("updated_at"),
        "fields": {},
    }
    for field, max_length in TEXT_FIELDS.items():
        value = item.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        if max_length is not None and len(value) > max_length:
            return None, f"{field} too long"
        valid["fields"][field] = value

    return valid, None


def sync_supplier_catalogue():
    """Fetch, validate and atomically reconcile the full supplier catalogue."""
    stats = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "rejected": [],
        "pages_fetched": 0,
        "requests_made": 0,
    }

    # Fetch every page before touching the database.
    raw_items = []
    cursor = None
    seen_cursors = set()

    while True:
        page = _fetch_page(cursor, stats)
        stats["pages_fetched"] += 1
        raw_items.extend(page["items"])

        next_cursor = page["next_cursor"]
        if next_cursor is None:
            break

        if not isinstance(next_cursor, str) or not next_cursor:
            raise SupplierSyncError("Supplier returned an invalid cursor.")

        if next_cursor in seen_cursors or next_cursor == cursor:
            raise SupplierSyncError("Supplier returned a repeated cursor.")

        seen_cursors.add(next_cursor)
        cursor = next_cursor

    # Validate records and retain the newest valid record per local SKU.
    latest_by_sku = {}

    for item in raw_items:
        valid, reason = _validate_item(item)

        if reason:
            supplier_sku = (
                item.get("supplier_sku")
                if isinstance(item, dict)
                else None
            )
            stats["rejected"].append({
                "supplier_sku": supplier_sku,
                "reason": reason,
            })
            continue

        sku = valid["sku"]
        previous = latest_by_sku.get(sku)

        if previous is None or _updated_key(valid["updated_at"]) > _updated_key(previous["updated_at"]):
            latest_by_sku[sku] = valid

    # No database writes occur until all pages have been fetched and validated.
    with transaction.atomic():
        existing = {
            product.sku: product
            for product in Product.objects.select_for_update().filter(
                sku__in=list(latest_by_sku)
            )
        }

        for sku, item in latest_by_sku.items():
            product = existing.get(sku)

            if product is None:
                Product.objects.create(
                    sku=sku,
                    name=item["fields"].get("name", ""),
                    category=item["fields"].get("category", ""),
                    brand=item["fields"].get("brand", ""),
                    vehicle_fitment=item["fields"].get("vehicle_fitment", ""),
                    description=item["fields"].get("description", ""),
                    price=item["price"],
                    stock=0,
                )
                stats["created"] += 1
                continue

            changed_fields = []

            new_values = {**item["fields"], "price": item["price"]}
            for field, value in new_values.items():
                if getattr(product, field) != value:
                    setattr(product, field, value)
                    changed_fields.append(field)

            if changed_fields:
                # Deliberately exclude stock.
                product.save(update_fields=changed_fields)
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

    return stats
