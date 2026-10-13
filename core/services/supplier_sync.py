
import time
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction

from core.models import Product


MAX_ATTEMPTS = 5
PAGE_SIZE = 20
MAX_PRICE = Decimal("9999999999.99")


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


def _validate_item(item):
    """Return (validated item, rejection reason)."""
    if not isinstance(item, dict):
        return None, "invalid item"

    sku = item.get("sku")
    if not isinstance(sku, str) or not sku.strip():
        return None, "missing sku"

    price_value = item.get("price")
    try:
        price = Decimal(str(price_value))
    except (InvalidOperation, ValueError, TypeError):
        return None, "invalid price"

    if not price.is_finite() or price <= 0 or price > MAX_PRICE:
        return None, "invalid price"

    if price.as_tuple().exponent < -2:
        return None, "invalid price"

    updated_at = item.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        return None, "missing updated_at"

    required_text_fields = (
        "name",
        "category",
        "brand",
        "vehicle_fitment",
        "description",
    )
    for field in required_text_fields:
        if not isinstance(item.get(field), str):
            return None, f"invalid {field}"

    return {
        "sku": sku.strip(),
        "name": item["name"],
        "category": item["category"],
        "brand": item["brand"],
        "vehicle_fitment": item["vehicle_fitment"],
        "description": item["description"],
        "price": price,
        "updated_at": updated_at,
    }, None


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

        if previous is None or valid["updated_at"] > previous["updated_at"]:
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
                    name=item["name"],
                    category=item["category"],
                    brand=item["brand"],
                    vehicle_fitment=item["vehicle_fitment"],
                    description=item["description"],
                    price=item["price"],
                    stock=0,
                )
                stats["created"] += 1
                continue

            changed_fields = []

            for field in (
                "name",
                "category",
                "brand",
                "vehicle_fitment",
                "description",
                "price",
            ):
                if getattr(product, field) != item[field]:
                    setattr(product, field, item[field])
                    changed_fields.append(field)

            if changed_fields:
                # Deliberately exclude stock.
                product.save(update_fields=changed_fields)
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

    return stats
