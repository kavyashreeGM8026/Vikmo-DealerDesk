"""Supplier sync (contract section 8) with the supplier mocked: no network, no real waiting."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from core.models import Product
from core.services import supplier_sync
from core.tests import assert_error_shape, make_product


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


def item(sku="SUP-1", price="100.00", updated_at="2026-10-01T10:00:00Z", **extra):
    data = {
        "supplier_sku": f"S-{sku}", "sku": sku, "name": f"Name {sku}", "category": "Brakes",
        "brand": "Endurance", "vehicle_fitment": "Bajaj Pulsar 150", "description": "desc",
        "price": price, "supplier_stock": 999, "updated_at": updated_at,
    }
    data.update(extra)
    return data


def page(items, next_cursor=None):
    return FakeResponse(200, {"items": items, "next_cursor": next_cursor})


@override_settings(SUPPLIER_API_URL="http://supplier.test", SUPPLIER_API_KEY="test-key")
class SupplierSyncTests(TestCase):
    def setUp(self):
        self.get = mock.patch("core.services.supplier_sync.requests.get").start()
        self.sleep = mock.patch("core.services.supplier_sync.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def sync(self, *responses):
        self.get.side_effect = list(responses)
        return supplier_sync.sync_supplier_catalogue()

    def test_creates_with_stock_zero_and_second_run_changes_nothing(self):
        feed = [page([item("SUP-1"), item("SUP-2", price="55.5")])]
        first = self.sync(*feed)
        self.assertEqual((first["created"], first["updated"], first["unchanged"]), (2, 0, 0))
        self.assertEqual(Product.objects.get(pk="SUP-1").stock, 0)
        second = self.sync(*feed)
        self.assertEqual((second["created"], second["updated"], second["unchanged"]), (0, 0, 2))

    def test_update_changes_catalogue_fields_but_never_stock(self):
        make_product(sku="SUP-1", price=Decimal("90.00"), stock=42, name="Old name")
        result = self.sync(page([item("SUP-1", price="100.00", supplier_stock=7)]))
        self.assertEqual((result["created"], result["updated"], result["unchanged"]), (0, 1, 0))
        product = Product.objects.get(pk="SUP-1")
        self.assertEqual((product.price, product.name, product.stock), (Decimal("100.00"), "Name SUP-1", 42))

    def test_follows_every_page(self):
        result = self.sync(page([item("SUP-1")], "c1"), page([item("SUP-2")], "c2"), page([item("SUP-3")]))
        self.assertEqual((result["created"], result["pages_fetched"], result["requests_made"]), (3, 3, 3))

    def test_invalid_items_are_rejected_and_reported_not_fatal(self):
        items = [
            item("OK-1"),
            item("", price="10.00"),                 # empty sku
            item("BAD-PRICE", price="-5"),           # not positive
            item("BAD-DEC", price="10.123"),         # more than 2 decimals
            item("NaN-1", price="abc"),              # not a number
            item("LONG-1", name="x" * 300),          # would not fit the column
        ]
        result = self.sync(page(items))
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(result["rejected"]), 5)
        self.assertEqual(Product.objects.filter(pk__in=["OK-1"]).count(), 1)

    def test_item_missing_optional_fields_is_not_rejected(self):
        bare = {"supplier_sku": "S-9", "sku": "SUP-9", "price": "12.00"}  # no text fields, no updated_at
        result = self.sync(page([bare]))
        self.assertEqual((result["created"], result["rejected"]), (1, []))
        self.assertEqual(Product.objects.get(pk="SUP-9").name, "")

    def test_absent_field_leaves_existing_value_unchanged(self):
        make_product(sku="SUP-1", price=Decimal("100.00"), name="Keep me", category="C", brand="B",
                     vehicle_fitment="V", description="D")
        result = self.sync(page([{"sku": "SUP-1", "price": "100.00"}]))
        self.assertEqual((result["updated"], result["unchanged"]), (0, 1))
        self.assertEqual(Product.objects.get(pk="SUP-1").name, "Keep me")

    def test_duplicate_sku_uses_latest_updated_at_whatever_the_page_order(self):
        newer = item("SUP-1", price="200.00", updated_at="2026-10-05T10:00:00+00:00")
        older = item("SUP-1", price="100.00", updated_at="2026-10-02T10:00:00Z")
        result = self.sync(page([newer], "c1"), page([older]))
        self.assertEqual((result["created"], result["updated"], result["unchanged"]), (1, 0, 0))
        self.assertEqual(Product.objects.get(pk="SUP-1").price, Decimal("200.00"))

    def test_rate_limit_waits_retry_after_then_succeeds(self):
        result = self.sync(FakeResponse(429, headers={"Retry-After": "3"}), page([item("SUP-1")]))
        self.sleep.assert_called_with(3)
        self.assertEqual((result["created"], result["requests_made"]), (1, 2))

    def test_server_error_then_success_is_retried(self):
        result = self.sync(FakeResponse(503), page([item("SUP-1")]))
        self.assertEqual((result["created"], result["requests_made"]), (1, 2))

    def test_each_request_is_attempted_at_most_five_times_and_nothing_is_written(self):
        make_product(sku="SUP-1", price=Decimal("90.00"))
        with self.assertRaises(supplier_sync.SupplierSyncError):
            self.sync(page([item("SUP-1", price="100.00")], "c1"), *[FakeResponse(503)] * 10)
        self.assertEqual(self.get.call_count, 1 + 5)
        self.assertEqual(Product.objects.get(pk="SUP-1").price, Decimal("90.00"))
        self.assertFalse(Product.objects.filter(pk="SUP-2").exists())

    def test_rejected_api_key_changes_nothing(self):
        with self.assertRaises(supplier_sync.SupplierSyncError):
            self.sync(FakeResponse(401))
        self.assertEqual(Product.objects.count(), 0)

    def test_endpoint_returns_502_supplier_unavailable(self):
        self.get.side_effect = [FakeResponse(503)] * 5
        response = self.client.post("/api/sync/supplier/")
        assert_error_shape(self, response, 502, "SUPPLIER_UNAVAILABLE")

    def test_endpoint_returns_counts(self):
        self.get.side_effect = [page([item("SUP-1")])]
        response = self.client.post("/api/sync/supplier/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual({k: body[k] for k in ("created", "updated", "unchanged", "rejected")},
                         {"created": 1, "updated": 0, "unchanged": 0, "rejected": []})
