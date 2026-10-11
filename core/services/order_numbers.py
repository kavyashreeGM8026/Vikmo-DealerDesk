"""Order numbers: ORD-YYYYMMDD-NNNN, the date being the day in Asia/Kolkata.

How uniqueness is guaranteed (PostgreSQL, no Python-level locks):

* There is one counter row per day in `core_ordernumbersequence`.
* Allocation is ONE atomic statement: INSERT ... ON CONFLICT (day) DO UPDATE SET
  last_value = last_value + 1 RETURNING last_value.
  Concurrent callers queue on that row's lock, so every caller gets a different value, in any
  process.
* The row lock is held until the surrounding transaction ends. Call this inside the same
  `transaction.atomic()` that inserts the order, as the last step before the insert: if the
  order insert fails, the counter increment rolls back too, so numbers have no gaps, and the
  lock is held for as short a time as possible.
* `Order.order_number` has a UNIQUE constraint as a final safety net.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import connection
from django.utils import timezone

from core.models import OrderNumberSequence

IST = ZoneInfo("Asia/Kolkata")


def _now():
    """Current instant (aware). Separate function so tests can pin the clock."""
    return timezone.now()


def order_day(now: datetime):
    """The calendar date in Asia/Kolkata, independent of the server's own timezone."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware.")
    return now.astimezone(IST).date()


def next_order_number(now: datetime = None) -> str:
    day = order_day(now or _now())
    table = connection.ops.quote_name(OrderNumberSequence._meta.db_table)
    sql = (
        f"INSERT INTO {table} (\"day\", \"last_value\") VALUES (%s, 1) "
        f"ON CONFLICT (\"day\") DO UPDATE SET \"last_value\" = {table}.\"last_value\" + 1 "
        f"RETURNING \"last_value\""
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [day])
        (sequence,) = cursor.fetchone()
    return f"ORD-{day:%Y%m%d}-{sequence:04d}"
