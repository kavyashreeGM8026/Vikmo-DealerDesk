"""Bounded retry for transient LLM-provider errors.

No Django and no SDK imports here, so it is easy to test. A provider error is transient when it
carries an HTTP-style `code` of 429/500/502/503/504 (the google-genai `APIError` classes do) or
is a timeout / connection failure. Everything else (400, 401, 403, 404, bad arguments,
programming errors) is permanent and is raised immediately, never retried.
"""
import logging
import time

logger = logging.getLogger(__name__)

TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_NAMES = frozenset({"ReadTimeout", "ConnectTimeout", "TimeoutException", "ConnectError"})
# Seconds to wait before attempt 2 and attempt 3 (so at most 3 attempts per call).
DEFAULT_DELAYS = (1.0, 2.0)
# A retry is only started if at least this many seconds of the budget would remain after the wait.
MIN_REMAINING = 5.0


def is_transient(exc) -> bool:
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool) and code in TRANSIENT_CODES:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in _TRANSIENT_NAMES


def call_with_retries(func, *, deadline, delays=DEFAULT_DELAYS, min_remaining=MIN_REMAINING,
                      sleep=time.sleep, now=time.monotonic):
    """Call `func()`; on a transient error wait and try again, within `deadline`
    (a `time.monotonic()` value). Raises the last error when retries or time run out."""
    attempt = 0
    while True:
        try:
            return func()
        except Exception as exc:
            if not is_transient(exc) or attempt >= len(delays):
                raise
            delay = delays[attempt]
            if now() + delay + min_remaining > deadline:
                logger.warning("Not retrying the LLM call: time budget nearly used up (%s)", exc)
                raise
            logger.warning("Transient LLM error (attempt %d of %d), retrying in %.1fs: %s",
                           attempt + 1, len(delays) + 1, delay, exc)
            sleep(delay)
            attempt += 1
