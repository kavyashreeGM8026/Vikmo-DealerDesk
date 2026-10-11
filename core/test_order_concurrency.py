"""Real-thread concurrency tests. Each thread uses its own PostgreSQL connection.

These need TransactionTestCase (not TestCase): the data must be committed so that the other
connections can see it. They exercise PostgreSQL's locking, so they only mean something
against PostgreSQL, which is the only database this project supports.
"""
import threading
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.db import connections, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from core.models import Order, OrderItem, OrderNumberSequence
from core.services.order_numbers import next_order_number
from core.tests import make_dealer, make_product

NOW = datetime(2026, 10, 12, 4, 45, tzinfo=dt_timezone.utc)


def run_in_threads(count, target):
    """Run `target(index)` in `count` threads released together; return (results, errors)."""
    barrier = threading.Barrier(count)
    results, errors = [], []

    def worker(index):
        try:
            barrier.wait(timeout=30)
            results.append(target(index))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertions below
            errors.append(repr(exc))
        finally:
            connections.close_all()  # each thread has its own connection

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return results, errors


class OrderNumberConcurrencyTests(TransactionTestCase):
    def test_parallel_allocations_are_unique_and_gap_free(self):
        threads, per_thread = 8, 5

        def allocate(_index):
            numbers = []
            for _ in range(per_thread):
                with transaction.atomic():
                    numbers.append(next_order_number(NOW))
            return numbers

        results, errors = run_in_threads(threads, allocate)
        self.assertEqual(errors, [])
        everything = [n for chunk in results for n in chunk]
        total = threads * per_thread
        self.assertEqual(len(everything), total)
        self.assertEqual(len(set(everything)), total, "duplicate order numbers were issued")
        self.assertEqual(sorted(everything), [f"ORD-20261012-{i:04d}" for i in range(1, total + 1)])
        self.assertEqual(OrderNumberSequence.objects.get().last_value, total)

    def test_parallel_api_creations_get_unique_numbers(self):
        dealer = make_dealer()
        make_product()
        threads, per_thread = 6, 3
        payload = {"dealer_id": dealer.id, "items": [{"sku": "BRK-1044", "quantity": 1}]}

        def create_orders(_index):
            client = APIClient()
            return [client.post("/api/orders/", payload, format="json") for _ in range(per_thread)]

        with mock.patch("core.services.order_numbers._now", return_value=NOW):
            results, errors = run_in_threads(threads, create_orders)
        self.assertEqual(errors, [])
        responses = [r for chunk in results for r in chunk]
        self.assertEqual([r.status_code for r in responses], [201] * (threads * per_thread))
        numbers = [r.json()["order_number"] for r in responses]
        total = threads * per_thread
        self.assertEqual(len(set(numbers)), total)
        self.assertEqual(sorted(numbers), [f"ORD-20261012-{i:04d}" for i in range(1, total + 1)])
        self.assertEqual(Order.objects.count(), total)
        self.assertEqual(OrderItem.objects.count(), total)
