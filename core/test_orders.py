"""Stage 2d-1: Order models, draft-order API (create / retrieve / list), pricing in the API."""
import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from core.models import Order, OrderItem, Product
from core.services.orders import create_draft_order
from core.tests import MONEY, assert_error_shape, make_dealer, make_product

D = Decimal
FIXED_NOW = datetime(2026, 10, 12, 4, 45, tzinfo=dt_timezone.utc)  # 10:15 IST on 2026-10-12
TODAY_PREFIX = "ORD-20261012-"
ORDER_NUMBER = re.compile(r"^ORD-\d{8}-\d{4}$")
ORDER_KEYS = {
    "order_number", "dealer_id", "status", "items", "subtotal", "tax_total", "grand_total",
    "created_at", "updated_at",
}
ITEM_KEYS = {"sku", "name", "quantity", "unit_price", "line_subtotal", "line_tax", "line_total"}


class OrderTestBase(APITestCase):
    """Pins the clock to 2026-10-12 IST so order numbers are predictable (and midnight-proof)."""

    def setUp(self):
        patcher = mock.patch("core.services.order_numbers._now", return_value=FIXED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dealer = make_dealer()
        self.brake = make_product()  # BRK-1044, 1459.50, stock 136
        self.filter = make_product(
            sku="FIL-1012", name="Oil Filter - KTM Duke 390", category="Filters",
            brand="Hiflo", vehicle_fitment="KTM Duke 390", price=D("349.00"), stock=40,
        )

    def payload(self, items=None, dealer_id=None):
        return {
            "dealer_id": dealer_id or self.dealer.id,
            "items": items if items is not None else [{"sku": "BRK-1044", "quantity": 3}],
        }

    def create(self, items=None, dealer_id=None):
        res = self.client.post("/api/orders/", self.payload(items, dealer_id), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()

    def stocks(self):
        return dict(Product.objects.values_list("sku", "stock"))


class OrderModelConstraintTests(OrderTestBase):
    def make_order(self, number="ORD-20261012-0001"):
        return Order.objects.create(order_number=number, dealer=self.dealer)

    def make_item(self, order, product, quantity=1):
        return OrderItem.objects.create(
            order=order, product=product, product_name=product.name, quantity=quantity,
            unit_price=product.price, line_subtotal=product.price, line_tax=D("0.00"),
            line_total=product.price,
        )

    def test_defaults(self):
        order = self.make_order()
        self.assertEqual(order.status, "DRAFT")
        self.assertEqual(order.grand_total, D("0.00"))
        self.assertIsNotNone(order.created_at)

    def test_order_number_is_unique(self):
        self.make_order()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_order()

    def test_status_must_be_a_known_value(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(order_number="ORD-X", dealer=self.dealer, status="BOGUS")

    def test_same_product_cannot_appear_twice_in_one_order(self):
        order = self.make_order()
        self.make_item(order, self.brake)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_item(order, self.brake, quantity=2)
        self.make_item(order, self.filter)  # a different product is fine
        self.assertEqual(order.items.count(), 2)

    def test_same_product_is_fine_in_different_orders(self):
        self.make_item(self.make_order("ORD-20261012-0001"), self.brake)
        self.make_item(self.make_order("ORD-20261012-0002"), self.brake)

    def test_quantity_must_be_at_least_one(self):
        order = self.make_order()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_item(order, self.brake, quantity=0)

    def test_dealer_and_product_are_protected_from_deletion(self):
        from django.db.models import ProtectedError

        order = self.make_order()
        self.make_item(order, self.brake)
        with self.assertRaises(ProtectedError):
            self.dealer.delete()
        with self.assertRaises(ProtectedError):
            self.brake.delete()


class OrderCreateTests(OrderTestBase):
    def test_create_returns_201_with_full_contract_shape(self):
        res = self.client.post("/api/orders/", self.payload(), format="json")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(set(body), ORDER_KEYS)
        self.assertRegex(body["order_number"], ORDER_NUMBER)
        self.assertEqual(body["order_number"], TODAY_PREFIX + "0001")
        self.assertEqual(body["dealer_id"], self.dealer.id)
        self.assertEqual(body["status"], "DRAFT")
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(set(item), ITEM_KEYS)
        self.assertEqual(item["sku"], "BRK-1044")
        self.assertEqual(item["name"], "Brake Pad Set - Bajaj Pulsar 150")
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["unit_price"], "1459.50")
        self.assertEqual(item["line_subtotal"], "4378.50")
        self.assertEqual(item["line_tax"], "788.13")
        self.assertEqual(item["line_total"], "5166.63")
        self.assertEqual(body["subtotal"], "4378.50")
        self.assertEqual(body["tax_total"], "788.13")
        self.assertEqual(body["grand_total"], "5166.63")

    def test_timestamps_are_iso_8601_in_ist(self):
        body = self.create()
        for key in ("created_at", "updated_at"):
            self.assertIsInstance(body[key], str)
            parsed = datetime.fromisoformat(body[key])
            self.assertIsNotNone(parsed.tzinfo)
            self.assertTrue(body[key].endswith("+05:30"), body[key])

    def test_order_is_persisted_with_items(self):
        body = self.create(items=[{"sku": "BRK-1044", "quantity": 3}, {"sku": "FIL-1012", "quantity": 2}])
        order = Order.objects.get(order_number=body["order_number"])
        self.assertEqual(order.dealer_id, self.dealer.id)
        self.assertEqual(order.status, "DRAFT")
        self.assertEqual(
            [(i.product_id, i.quantity, i.product_name) for i in order.items.all()],
            [("BRK-1044", 3, "Brake Pad Set - Bajaj Pulsar 150"), ("FIL-1012", 2, "Oil Filter - KTM Duke 390")],
        )
        self.assertEqual([i["sku"] for i in body["items"]], ["BRK-1044", "FIL-1012"])  # request order

    def test_creating_a_draft_does_not_change_stock(self):
        before = self.stocks()
        self.create(items=[{"sku": "BRK-1044", "quantity": 100}, {"sku": "FIL-1012", "quantity": 40}])
        self.assertEqual(self.stocks(), before)

    def test_draft_may_exceed_available_stock(self):
        # Stock is only checked on confirm (Stage 2d-2), never for a draft.
        body = self.create(items=[{"sku": "BRK-1044", "quantity": 100000}])
        self.assertEqual(body["status"], "DRAFT")
        self.assertEqual(self.stocks()["BRK-1044"], 136)

    def test_missing_or_unknown_dealer_is_400(self):
        for payload in [
            {"items": [{"sku": "BRK-1044", "quantity": 1}]},
            self.payload(dealer_id=self.dealer.id + 1000),
        ]:
            assert_error_shape(self, self.client.post("/api/orders/", payload, format="json"), 400, "VALIDATION_ERROR")
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_dealer_id_types_are_400(self):
        for bad in ["7", True, False, 1.5, 7.0, 0, -1, None, [], {}]:
            payload = {"dealer_id": bad, "items": [{"sku": "BRK-1044", "quantity": 1}]}
            assert_error_shape(self, self.client.post("/api/orders/", payload, format="json"), 400, "VALIDATION_ERROR")
        self.assertEqual(Order.objects.count(), 0)

    def test_unknown_sku_is_400_and_named(self):
        res = self.client.post("/api/orders/", self.payload(items=[{"sku": "NOPE-0000", "quantity": 1}]), format="json")
        error = assert_error_shape(self, res, 400, "VALIDATION_ERROR")
        self.assertIn("NOPE-0000", error["message"])
        self.assertEqual(Order.objects.count(), 0)

    def test_one_unknown_sku_among_valid_ones_creates_nothing(self):
        items = [{"sku": "BRK-1044", "quantity": 1}, {"sku": "NOPE-0000", "quantity": 1}]
        assert_error_shape(self, self.client.post("/api/orders/", self.payload(items=items), format="json"), 400, "VALIDATION_ERROR")
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)

    def test_unknown_dealer_and_unknown_sku_are_both_reported(self):
        payload = {"dealer_id": self.dealer.id + 1000, "items": [{"sku": "NOPE-0000", "quantity": 1}]}
        error = assert_error_shape(self, self.client.post("/api/orders/", payload, format="json"), 400, "VALIDATION_ERROR")
        self.assertEqual({d["field"] for d in error["details"]}, {"dealer_id", "items"})

    def test_missing_null_or_empty_items_is_400(self):
        for payload in [
            {"dealer_id": self.dealer.id},
            {"dealer_id": self.dealer.id, "items": None},
            {"dealer_id": self.dealer.id, "items": []},
            {"dealer_id": self.dealer.id, "items": "BRK-1044"},
            {"dealer_id": self.dealer.id, "items": {"sku": "BRK-1044", "quantity": 1}},
            {"dealer_id": self.dealer.id, "items": ["BRK-1044"]},
            {"dealer_id": self.dealer.id, "items": [{}]},
            {"dealer_id": self.dealer.id, "items": [{"quantity": 1}]},
            {"dealer_id": self.dealer.id, "items": [{"sku": "", "quantity": 1}]},
            {"dealer_id": self.dealer.id, "items": [{"sku": None, "quantity": 1}]},
            {},
            [],
        ]:
            assert_error_shape(self, self.client.post("/api/orders/", payload, format="json"), 400, "VALIDATION_ERROR")
        self.assertEqual(Order.objects.count(), 0)

    def test_malformed_json_is_400(self):
        res = self.client.post("/api/orders/", "{not json", content_type="application/json")
        assert_error_shape(self, res, 400, "VALIDATION_ERROR")

    def test_invalid_quantities_are_400(self):
        for bad in [0, -1, -100, True, False, "3", 3.0, 2.5, None, [], 2**31]:
            items = [{"sku": "BRK-1044", "quantity": bad}]
            res = self.client.post("/api/orders/", self.payload(items=items), format="json")
            assert_error_shape(self, res, 400, "VALIDATION_ERROR")
        missing = self.client.post("/api/orders/", self.payload(items=[{"sku": "BRK-1044"}]), format="json")
        assert_error_shape(self, missing, 400, "VALIDATION_ERROR")
        self.assertEqual(Order.objects.count(), 0)

    def test_quantity_one_is_the_smallest_valid_quantity(self):
        self.assertEqual(self.create(items=[{"sku": "BRK-1044", "quantity": 1}])["items"][0]["quantity"], 1)

    def test_duplicate_sku_is_400(self):
        items = [{"sku": "BRK-1044", "quantity": 1}, {"sku": "BRK-1044", "quantity": 2}]
        error = assert_error_shape(self, self.client.post("/api/orders/", self.payload(items=items), format="json"), 400, "VALIDATION_ERROR")
        self.assertIn("BRK-1044", error["message"])
        self.assertEqual(Order.objects.count(), 0)

    def test_order_whose_total_does_not_fit_is_400_not_500(self):
        Product.objects.filter(pk="BRK-1044").update(price=D("9999999999.99"))
        items = [{"sku": "BRK-1044", "quantity": 2_147_483_647}]
        assert_error_shape(self, self.client.post("/api/orders/", self.payload(items=items), format="json"), 400, "VALIDATION_ERROR")
        self.assertEqual(Order.objects.count(), 0)

    def test_idempotency_key_header_is_not_implemented_yet(self):
        # Stage 2d-3. For now the header is simply ignored: two posts make two orders.
        headers = {"HTTP_IDEMPOTENCY_KEY": "k1"}
        self.client.post("/api/orders/", self.payload(), format="json", **headers)
        self.client.post("/api/orders/", self.payload(), format="json", **headers)
        self.assertEqual(Order.objects.count(), 2)


class OrderPricingApiTests(OrderTestBase):
    def tiny(self, sku, price):
        return make_product(sku=sku, name=f"Tiny {sku}", price=D(price), stock=10)

    def test_unit_price_comes_from_the_catalogue(self):
        Product.objects.filter(pk="BRK-1044").update(price=D("1500.00"))
        item = self.create()["items"][0]
        self.assertEqual(item["unit_price"], "1500.00")
        self.assertEqual(item["line_subtotal"], "4500.00")

    def test_multi_line_totals(self):
        body = self.create(items=[{"sku": "BRK-1044", "quantity": 3}, {"sku": "FIL-1012", "quantity": 2}])
        self.assertEqual([i["line_subtotal"] for i in body["items"]], ["4378.50", "698.00"])
        self.assertEqual([i["line_tax"] for i in body["items"]], ["788.13", "125.64"])
        self.assertEqual([i["line_total"] for i in body["items"]], ["5166.63", "823.64"])
        self.assertEqual(body["subtotal"], "5076.50")
        self.assertEqual(body["tax_total"], "913.77")
        self.assertEqual(body["grand_total"], "5990.27")

    def test_half_up_rounding_in_the_api(self):
        self.tiny("TST-0001", "0.25")  # tax 0.045 -> 0.05
        self.tiny("TST-0002", "10.25")  # tax 1.845 -> 1.85
        body = self.create(items=[{"sku": "TST-0001", "quantity": 1}, {"sku": "TST-0002", "quantity": 1}])
        self.assertEqual(body["items"][0]["line_tax"], "0.05")
        self.assertEqual(body["items"][1]["line_tax"], "1.85")
        self.assertEqual(body["tax_total"], "1.90")

    def test_each_line_tax_is_rounded_independently(self):
        self.tiny("TST-0001", "0.25")
        self.tiny("TST-0002", "0.25")
        body = self.create(items=[{"sku": "TST-0001", "quantity": 1}, {"sku": "TST-0002", "quantity": 1}])
        self.assertEqual([i["line_tax"] for i in body["items"]], ["0.05", "0.05"])
        self.assertEqual(body["subtotal"], "0.50")
        self.assertEqual(body["tax_total"], "0.10")  # not 0.09 (rounding the total instead)
        self.assertEqual(body["grand_total"], "0.60")

    def test_every_money_value_is_a_two_decimal_string(self):
        self.tiny("TST-0003", "100")
        body = self.create(items=[{"sku": "TST-0003", "quantity": 1}, {"sku": "BRK-1044", "quantity": 1}])
        values = [body["subtotal"], body["tax_total"], body["grand_total"]]
        for item in body["items"]:
            values += [item["unit_price"], item["line_subtotal"], item["line_tax"], item["line_total"]]
        for value in values:
            self.assertIsInstance(value, str)
            self.assertRegex(value, MONEY)
        self.assertEqual(body["items"][0]["unit_price"], "100.00")

    def test_draft_responses_use_current_catalogue_prices_and_names(self):
        body = self.create()
        number = body["order_number"]
        self.assertEqual(body["grand_total"], "5166.63")

        patch = self.client.patch(
            "/api/products/BRK-1044/", {"price": "1500.00", "name": "Brake Pad Set v2"}, format="json"
        )
        self.assertEqual(patch.status_code, 200)

        item = self.client.get(f"/api/orders/{number}/").json()["items"][0]
        self.assertEqual(item["unit_price"], "1500.00")
        self.assertEqual(item["name"], "Brake Pad Set v2")
        self.assertEqual(item["line_subtotal"], "4500.00")
        self.assertEqual(item["line_tax"], "810.00")
        self.assertEqual(item["line_total"], "5310.00")
        updated = self.client.get(f"/api/orders/{number}/").json()
        self.assertEqual((updated["subtotal"], updated["tax_total"], updated["grand_total"]), ("4500.00", "810.00", "5310.00"))

        listed = self.client.get("/api/orders/").json()["results"][0]
        self.assertEqual(listed["grand_total"], "5310.00")

    def test_reading_a_draft_never_writes_to_the_database(self):
        number = self.create()["order_number"]
        Product.objects.filter(pk="BRK-1044").update(price=D("1500.00"))
        order_before = Order.objects.values().get(order_number=number)
        item_before = list(OrderItem.objects.values())

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(f"/api/orders/{number}/").status_code, 200)
            self.assertEqual(self.client.get("/api/orders/").status_code, 200)
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        # the stored snapshot is untouched (still the creation-time price and totals)
        self.assertEqual(Order.objects.values().get(order_number=number), order_before)
        self.assertEqual(list(OrderItem.objects.values()), item_before)
        self.assertEqual(Order.objects.get(order_number=number).subtotal, D("4378.50"))

    def test_non_draft_orders_use_stored_prices_and_name_snapshot(self):
        # Confirmation itself is Stage 2d-2; here the status is set directly to prove that
        # anything that is not a draft is served from the stored values.
        body = self.create()
        number = body["order_number"]
        Order.objects.filter(order_number=number).update(status="CONFIRMED")
        self.client.patch("/api/products/BRK-1044/", {"price": "9999.00", "name": "Renamed"}, format="json")
        again = self.client.get(f"/api/orders/{number}/").json()
        self.assertEqual(again["status"], "CONFIRMED")
        self.assertEqual(again["items"], body["items"])
        self.assertEqual(again["grand_total"], "5166.63")
        self.assertEqual(again["items"][0]["name"], "Brake Pad Set - Bajaj Pulsar 150")


class OrderRetrieveAndListTests(OrderTestBase):
    def test_get_order_by_number_matches_the_create_response(self):
        created = self.create()
        res = self.client.get(f"/api/orders/{created['order_number']}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), created)

    def test_unknown_order_is_404(self):
        assert_error_shape(self, self.client.get("/api/orders/ORD-20261012-9999/"), 404, "NOT_FOUND")

    def test_malformed_order_numbers_are_404_not_500(self):
        for bad in ["abc", "ORD-1", "ORD-20261012-", "1", "ord-20261012-0001", "ORD-20261012-0001%00", "%00"]:
            assert_error_shape(self, self.client.get(f"/api/orders/{bad}/"), 404, "NOT_FOUND")

    def make_orders(self, count, dealer=None):
        return [
            create_draft_order(dealer or self.dealer, [(self.brake, 1)]) for _ in range(count)
        ]

    def test_list_shape_and_pagination(self):
        self.make_orders(25)
        first = self.client.get("/api/orders/").json()
        self.assertEqual(set(first), {"count", "next", "previous", "results"})
        self.assertEqual(first["count"], 25)
        self.assertEqual(len(first["results"]), 20)
        self.assertIsNotNone(first["next"])
        self.assertIsNone(first["previous"])
        for row in first["results"]:
            self.assertEqual(set(row), ORDER_KEYS)
            self.assertEqual(set(row["items"][0]), ITEM_KEYS)

        second = self.client.get("/api/orders/?page=2").json()
        self.assertEqual(len(second["results"]), 5)
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])

        numbers = [r["order_number"] for r in first["results"] + second["results"]]
        self.assertEqual(len(set(numbers)), 25)  # nothing repeated or missing across pages

        small = self.client.get("/api/orders/?page_size=3").json()
        self.assertEqual(len(small["results"]), 3)

    def test_page_size_is_capped_at_100(self):
        self.make_orders(3)
        self.assertEqual(len(self.client.get("/api/orders/?page_size=1000").json()["results"]), 3)

    def test_page_out_of_range_is_json_404(self):
        assert_error_shape(self, self.client.get("/api/orders/?page=99"), 404, "NOT_FOUND")

    def test_list_is_newest_first_and_stable(self):
        orders = self.make_orders(5)
        expected = [o.order_number for o in reversed(orders)]
        for _ in range(2):
            got = [r["order_number"] for r in self.client.get("/api/orders/").json()["results"]]
            self.assertEqual(got, expected)

    def test_filter_by_dealer_id(self):
        other = make_dealer(dealer_code="DLR-008", name="Other Dealer")
        self.make_orders(3)
        self.make_orders(2, dealer=other)
        mine = self.client.get(f"/api/orders/?dealer_id={self.dealer.id}").json()
        self.assertEqual(mine["count"], 3)
        self.assertEqual({r["dealer_id"] for r in mine["results"]}, {self.dealer.id})
        theirs = self.client.get(f"/api/orders/?dealer_id={other.id}").json()
        self.assertEqual(theirs["count"], 2)
        nobody = self.client.get(f"/api/orders/?dealer_id={other.id + 1000}").json()
        self.assertEqual((nobody["count"], nobody["results"]), (0, []))

    def test_filter_by_status(self):
        orders = self.make_orders(4)
        Order.objects.filter(order_number__in=[orders[0].order_number, orders[1].order_number]).update(status="CONFIRMED")
        confirmed = self.client.get("/api/orders/?status=CONFIRMED").json()
        self.assertEqual(confirmed["count"], 2)
        self.assertEqual({r["status"] for r in confirmed["results"]}, {"CONFIRMED"})
        drafts = self.client.get("/api/orders/?status=DRAFT").json()
        self.assertEqual(drafts["count"], 2)
        self.assertEqual(self.client.get("/api/orders/?status=DELIVERED").json()["count"], 0)

    def test_filters_combine(self):
        other = make_dealer(dealer_code="DLR-008", name="Other Dealer")
        mine = self.make_orders(2)
        self.make_orders(2, dealer=other)
        Order.objects.filter(order_number=mine[0].order_number).update(status="CANCELLED")
        body = self.client.get(f"/api/orders/?dealer_id={self.dealer.id}&status=CANCELLED").json()
        self.assertEqual([r["order_number"] for r in body["results"]], [mine[0].order_number])

    def test_invalid_filters_are_400(self):
        for query in ["dealer_id=abc", "dealer_id=-1", "dealer_id=1.5", "dealer_id=99999999999999999999",
                      "status=SHIPPED", "status=draft"]:
            assert_error_shape(self, self.client.get(f"/api/orders/?{query}"), 400, "VALIDATION_ERROR")

    def test_empty_filter_values_are_ignored(self):
        self.make_orders(2)
        self.assertEqual(self.client.get("/api/orders/?status=&dealer_id=").json()["count"], 2)

    def test_list_query_count_does_not_grow_with_the_number_of_orders(self):
        self.make_orders(2)
        with CaptureQueriesContext(connection) as small:
            self.client.get("/api/orders/")
        self.make_orders(6)
        with CaptureQueriesContext(connection) as large:
            self.client.get("/api/orders/")
        self.assertEqual(len(small), len(large))  # no N+1


class OrderFlowDoesNotDisturbOtherStagesTests(OrderTestBase):
    def test_dealers_products_and_inventory_still_work_next_to_orders(self):
        self.create()
        self.assertEqual(self.client.get("/api/dealers/").json()["count"], 1)
        self.assertEqual(self.client.get("/api/products/BRK-1044/").json()["stock"], 136)
        res = self.client.put("/api/inventory/BRK-1044/", {"quantity": 25, "reason": "count"}, format="json")
        self.assertEqual((res.status_code, res.json()), (200, {"sku": "BRK-1044", "quantity": 25}))
        self.assertEqual(self.client.get("/api/health/").json(), {"status": "ok"})

    def test_manual_inventory_update_does_not_alter_an_existing_order(self):
        body = self.create()
        self.client.put("/api/inventory/BRK-1044/", {"quantity": 0}, format="json")
        again = self.client.get(f"/api/orders/{body['order_number']}/").json()
        self.assertEqual(again, body)
        self.assertEqual(Order.objects.count(), 1)


class InventoryRowLockingTests(OrderTestBase):
    def test_inventory_update_locks_the_product_row(self):
        with CaptureQueriesContext(connection) as queries:
            res = self.client.put("/api/inventory/BRK-1044/", {"quantity": 25}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            any("FOR UPDATE" in q["sql"].upper() for q in queries),
            "PUT /api/inventory/{sku}/ must read the product with SELECT ... FOR UPDATE",
        )

    def test_inventory_error_behaviour_is_unchanged(self):
        assert_error_shape(self, self.client.put("/api/inventory/NOPE-0000/", {"quantity": 1}, format="json"), 404, "NOT_FOUND")
        assert_error_shape(self, self.client.put("/api/inventory/BRK-1044/", {"quantity": -1}, format="json"), 400, "VALIDATION_ERROR")
        # unknown SKU is reported before a bad body, exactly as before
        assert_error_shape(self, self.client.put("/api/inventory/NOPE-0000/", {"quantity": -1}, format="json"), 404, "NOT_FOUND")
        self.assertEqual(Product.objects.get(pk="BRK-1044").stock, 136)
