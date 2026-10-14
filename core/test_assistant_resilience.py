"""Retry rules for transient LLM errors (no Django, no network, no real waiting)."""
import unittest

from core.services.assistant.resilience import call_with_retries, is_transient


class FakeError(Exception):
    def __init__(self, code):
        super().__init__(f"error {code}")
        self.code = code


class Flaky:
    """Fails with the given errors in order, then returns 'ok'."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class IsTransientTests(unittest.TestCase):
    def test_rate_limit_and_server_errors_are_transient(self):
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(is_transient(FakeError(code)), code)

    def test_client_errors_are_permanent(self):
        for code in (400, 401, 403, 404):
            self.assertFalse(is_transient(FakeError(code)), code)

    def test_timeouts_and_connection_errors_are_transient(self):
        self.assertTrue(is_transient(TimeoutError()))
        self.assertTrue(is_transient(ConnectionError()))

    def test_other_errors_are_permanent(self):
        self.assertFalse(is_transient(ValueError("bad")))
        self.assertFalse(is_transient(RuntimeError("boom")))


class CallWithRetriesTests(unittest.TestCase):
    def run_it(self, func, deadline=50.0):
        clock = Clock()
        result = call_with_retries(func, deadline=deadline, sleep=clock.sleep, now=clock.now)
        return result, clock

    def test_success_needs_no_retry(self):
        func = Flaky()
        result, clock = self.run_it(func)
        self.assertEqual((result, func.calls, clock.sleeps), ("ok", 1, []))

    def test_transient_failure_then_success(self):
        func = Flaky(FakeError(503))
        result, clock = self.run_it(func)
        self.assertEqual((result, func.calls, clock.sleeps), ("ok", 2, [1.0]))

    def test_two_failures_then_success_uses_backoff(self):
        func = Flaky(FakeError(429), FakeError(500))
        result, clock = self.run_it(func)
        self.assertEqual((result, func.calls, clock.sleeps), ("ok", 3, [1.0, 2.0]))

    def test_retries_are_bounded_to_three_attempts(self):
        func = Flaky(*[FakeError(503) for _ in range(10)])
        with self.assertRaises(FakeError):
            self.run_it(func)
        self.assertEqual(func.calls, 3)

    def test_permanent_error_is_not_retried(self):
        func = Flaky(FakeError(400))
        with self.assertRaises(FakeError):
            self.run_it(func)
        self.assertEqual(func.calls, 1)

    def test_no_retry_when_the_time_budget_is_nearly_used_up(self):
        func = Flaky(FakeError(503))
        with self.assertRaises(FakeError):
            self.run_it(func, deadline=5.5)  # 0 + 1s wait + 5s reserve > 5.5
        self.assertEqual(func.calls, 1)


if __name__ == "__main__":
    unittest.main()
