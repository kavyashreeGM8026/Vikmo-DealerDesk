"""Forecast lookup and reorder suggestions (contract section 10).

Everything that does arithmetic is a plain function over plain data, so it can be tested
without a database. Only `current_stocks` touches the DB, and only to read.

Precision: forecast values are kept as Decimal (read straight from the CSV text) and the
safety-stock formula is evaluated exactly with Fractions, so the two `ceil` steps never
suffer from float noise such as 30.000000000000004 -> 31.
"""
import csv
import functools
import logging
from decimal import Decimal
from fractions import Fraction
from math import isqrt
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_LEVEL_Z = Fraction(165, 100)  # 1.65
RECENT_WEEKS = 12


# ---------------------------------------------------------------- file loading

def forecast_path() -> Path:
    from django.conf import settings
    return Path(settings.BASE_DIR) / "ml" / "forecast.csv"


def data_path(name: str) -> Path:
    from django.conf import settings
    return Path(settings.DATA_DIR) / name


@functools.lru_cache(maxsize=16)
def _read_rows(path: str, mtime_ns: int):
    with open(path, newline="", encoding="utf-8") as fh:
        return tuple(csv.DictReader(fh))


def _rows(path: Path, required: list):
    """Rows of a CSV, cached per file version (re-read automatically if the file changes)."""
    try:
        rows = _read_rows(str(path), path.stat().st_mtime_ns)
    except FileNotFoundError:
        logger.error("Required data file missing: %s", path)
        raise RuntimeError(f"Required data file is missing: {path.name}")
    if rows and not set(required) <= set(rows[0]):
        raise RuntimeError(f"{path.name} is missing columns {sorted(set(required) - set(rows[0]))}")
    return rows


def load_lead_times() -> dict:
    """{sku: lead_time_weeks}, in file order. This file defines the forecast SKUs."""
    rows = _rows(data_path("lead_times.csv"), ["sku", "lead_time_weeks"])
    return {r["sku"].strip(): int(r["lead_time_weeks"]) for r in rows}


def load_forecast() -> dict:
    """{sku: [(week_start 'YYYY-MM-DD', Decimal forecast_units), ...]} in chronological order."""
    rows = _rows(forecast_path(), ["sku", "week_start", "forecast_units"])
    out = {}
    for r in rows:
        out.setdefault(r["sku"].strip(), []).append((r["week_start"].strip(), Decimal(r["forecast_units"])))
    return {sku: sorted(v) for sku, v in out.items()}  # ISO dates sort chronologically


def load_history() -> dict:
    """{sku: [units_sold, ...]} ordered oldest -> newest (Fractions, exact)."""
    rows = _rows(data_path("sales_history.csv"), ["week_start", "sku", "units_sold"])
    out = {}
    for r in sorted(rows, key=lambda r: r["week_start"]):
        out.setdefault(r["sku"].strip(), []).append(Fraction(r["units_sold"]))
    return out


# --------------------------------------------------------------------- the maths

def safety_stock(history: list, lead_time_weeks: int) -> int:
    """ceil(1.65 * sigma * sqrt(L)) with sigma the SAMPLE std dev (n-1) of the most recent
    12 weeks (all weeks if fewer). Fewer than 2 observations have no sample std dev, so
    sigma is taken as 0 (safety stock 0); the reorder point then equals lead-time demand.

    sigma*1.65*sqrt(L) = sqrt(Q) with Q = 1.65^2 * L * variance (a rational number), so
    ceil(sqrt(Q)) is found with integer arithmetic: the smallest m with m*m >= Q.
    """
    recent = list(history)[-RECENT_WEEKS:]
    n = len(recent)
    if n < 2:
        return 0
    mean = sum(recent) / n
    variance = sum((x - mean) ** 2 for x in recent) / (n - 1)
    q = SERVICE_LEVEL_Z ** 2 * lead_time_weeks * variance
    m = isqrt(q.numerator // q.denominator)
    return m if m * m >= q else m + 1


def ceil_decimal(x: Decimal) -> int:
    return int(x.to_integral_value(rounding="ROUND_CEILING"))


def lead_time_demand(forecast: list, lead_time_weeks: int) -> Decimal:
    """Sum of the first L chronological forecast weeks, unrounded."""
    if len(forecast) < lead_time_weeks:
        raise RuntimeError(f"Forecast has {len(forecast)} weeks but lead time is {lead_time_weeks}")
    return sum((units for _week, units in forecast[:lead_time_weeks]), Decimal(0))


def reorder_entry(sku: str, lead_weeks: int, forecast: list, history: list, stock: int) -> dict:
    demand = lead_time_demand(forecast, lead_weeks)
    ss = safety_stock(history, lead_weeks)
    point = demand + ss
    return {
        "sku": sku,
        "current_stock": stock,
        "lead_time_weeks": lead_weeks,
        "lead_time_demand": float(demand),
        "safety_stock": ss,
        "reorder_point": float(point),
        "reorder_qty": max(0, ceil_decimal(point - stock)),
    }


# ------------------------------------------------------------------ DB + entry points

def current_stocks(skus) -> dict:
    """Live stock for `skus` in ONE read-only query. SKUs with no Product row are absent."""
    from core.models import Product
    return dict(Product.objects.filter(sku__in=list(skus)).values_list("sku", "stock"))


def get_forecast(sku: str):
    """The 4 forecast rows for a forecast SKU, or None if the SKU is not a forecast SKU."""
    if sku not in load_lead_times():
        return None
    rows = load_forecast().get(sku)
    if not rows:
        return None
    return [{"week_start": w, "forecast_units": float(u)} for w, u in rows]


def reorder_suggestions() -> list:
    lead = load_lead_times()
    forecast, history = load_forecast(), load_history()
    stocks = current_stocks(lead)
    # A forecast SKU with no Product row is treated as 0 stock (it may still need reordering).
    return [
        reorder_entry(sku, weeks, forecast[sku], history.get(sku, []), stocks.get(sku, 0))
        for sku, weeks in lead.items()
    ]
