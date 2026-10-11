"""Order-number allocation: format, IST dates, per-day sequence, rollback behaviour."""
import time
import unittest
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import transaction  
from rest_framework.test import APITestCase

from core.models import Order, OrderNumberSequence
from core.services.order_numbers import next_order_number
from core.services.orders import create_draft_order
from core.tests import assert_error_shape, make_dealer, make_product


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class NextOrderNumberTests(APITestCase):
    def test_format_and_sequence_start_at_0001(self):
        now = utc(2026, 10, 12, 4, 45)
        self.assertEqual(
            [next_order_number(now) for _ in range(3)],
            ["ORD-20261012-0001", "ORD-20261012-0002", "ORD-20261012-0003"],
        )

    def test_each_day_starts_again_at_0001(self):
        self.assertEqual(next_order_number(utc(2026, 10, 12, 4, 0)), "ORD-20261012-0001")
        self.assertEqual(next_order_number(utc(2026, 10, 12, 5, 0)), "ORD-20261012-0002")
        self.assertEqual(next_order_number(utc(2026, 10, 13, 5, 0)), "ORD-20261013-0001")
        self.assertEqual(next_order_number(utc(2026, 10, 12, 6, 0)), "ORD-20261012-0003")  # back to day 1

    def test_date_is_the_asia_kolkata_date_not_utc(self):
        # 20:00 UTC on the 11th is already 01:30 on the 12th in India
        self.assertEqual(next_order_number(utc(2026, 10, 11, 20, 0)), "ORD-20261012-0001")
        # 18:29 UTC = 23:59 IST: still the 12th ...
        self.assertEqual(next_order_number(utc(2026, 10, 12, 18, 29)), "ORD-20261012-0002")
        # ... and 18:30 UTC = 00:00 IST: the 13th
        self.assertEqual(next_order_number(utc(2026, 10, 12, 18, 30)), "ORD-20261013-0001")

    @unittest.skipUnless(hasattr(time, "tzset"), "time.tzset is not available on Windows")
    def test_does_not_depend_on_the_servers_timezone(self):
        import os

        old = os.environ.get("TZ")
        try:
            for tz in ("UTC", "America/Los_Angeles", "Pacific/Auckland"):
                os.environ["TZ"] = tz
                time.tzset()
                OrderNumberSequence.objects.all().delete()
                self.assertEqual(next_order_number(utc(2026, 10, 11, 20, 0)), "ORD-20261012-0001", tz)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            next_order_number(datetime(2026, 10, 12, 10, 0))

    def test_a_rolled_back_transaction_leaves_no_gap(self):
        now = utc(2026, 10, 12, 4, 45)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.assertEqual(next_order_number(now), "ORD-20261012-0001")
                raise RuntimeError("order insert failed")
        self.assertEqual(next_order_number(now), "ORD-20261012-0001")

    def test_counter_row_is_one_per_day(self):
        for _ in range(3):
            next_order_number(utc(2026, 10, 12, 4, 45))
        row = OrderNumberSequence.objects.get()
        self.assertEqual(row.last_value, 3)
        self.assertEqual(str(row.day), "2026-10-12")


class OrderNumbersThroughTheApiTests(APITestCase):
    def setUp(self):
        patcher = mock.patch("core.services.order_numbers._now", return_value=utc(2026, 10, 12, 4, 45))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dealer = make_dealer()
        make_product()

    def post(self):
        return self.client.post(
            "/api/orders/",
            {"dealer_id": self.dealer.id, "items": [{"sku": "BRK-1044", "quantity": 1}]},
            format="json",
        )

    def test_orders_get_unique_consecutive_numbers(self):
        numbers = [self.post().json()["order_number"] for _ in range(5)]
        self.assertEqual(numbers, [f"ORD-20261012-{i:04d}" for i in range(1, 6)])
        self.assertEqual(Order.objects.values("order_number").distinct().count(), 5)

    def test_rejected_requests_do_not_consume_numbers(self):
        bad = self.client.post("/api/orders/", {"dealer_id": self.dealer.id, "items": []}, format="json")
        assert_error_shape(self, bad, 400, "VALIDATION_ERROR")
        self.assertEqual(self.post().json()["order_number"], "ORD-20261012-0001")

    def test_a_failed_order_insert_rolls_the_counter_back(self):
        product = make_product(sku="FIL-1012", name="Filter", price=Decimal("10.00"))
        with mock.patch("core.services.orders.OrderItem.objects.bulk_create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                create_draft_order(self.dealer, [(product, 1)])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(self.post().json()["order_number"], "ORD-20261012-0001")
