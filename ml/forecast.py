"""VIKMO DealerDesk - 4-week demand forecast (Stage 1 of the AI/ML track).

Regenerates ml/forecast.csv from data/*.csv, backtests the method against
simple baselines, and validates the output file.

    python ml/forecast.py                 # backtest + write + validate
    python ml/forecast.py --skip-backtest # write + validate only
    python ml/forecast.py --backtest-only # print backtest, write nothing

Method (see ml/README.md): a pooled log-linear model (SKU level + trend +
promotion + distance-to-Diwali effects) whose per-SKU level is an exponentially
weighted average of recent residuals. Rarely-sold SKUs use a plain average.
Only pandas / numpy are required.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "ml" / "forecast.csv"

FORECAST_WEEKS = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]
HORIZON = len(FORECAST_WEEKS)
K_MIN, K_MAX = -3, 5          # Diwali-distance window (weeks) that gets its own effect
INTERMITTENT_ZERO_SHARE = 0.2  # SKUs with more zero weeks than this use the simple method
MIN_FIT_WEEKS = 8             # SKUs with fewer weeks are not used to fit global effects


@dataclass(frozen=True)
class Config:
    half_life: float = 4.0     # weeks, EWMA half-life for the SKU level
    window: int = 13           # max weeks of history used for the level
    seasonal: bool = False     # add one yearly sin/cos harmonic
    trend: bool = True         # global linear trend (log scale)
    smear: bool = False        # multiply by mean(exp(resid)) to target the mean, not the median
    intermittent: str = "mean"  # "mean" or "median" of last `window` weeks


DEFAULT = Config()

# ---------------------------------------------------------------- loading ---

def _read(name: str, cols: list[str]) -> pd.DataFrame:
    path = DATA / name
    if not path.exists():
        sys.exit(f"ERROR: missing input file {path}")
    df = pd.read_csv(path)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        sys.exit(f"ERROR: {name} is missing columns {missing}; found {list(df.columns)}")
    return df


def load_inputs():
    sales = _read("sales_history.csv", ["week_start", "sku", "units_sold", "promo_flag"])
    promo = _read("promo_calendar.csv", ["week_start", "sku", "promo_flag"])
    lead = _read("lead_times.csv", ["sku", "lead_time_weeks"])
    fest = _read("festival_calendar.csv", ["festival", "date"])
    for df, col in ((sales, "week_start"), (promo, "week_start"), (fest, "date")):
        df[col] = pd.to_datetime(df[col], errors="coerce")
        if df[col].isna().any():
            sys.exit(f"ERROR: unparseable dates in column {col}")
    sales["units_sold"] = pd.to_numeric(sales["units_sold"], errors="coerce")
    if sales["units_sold"].isna().any() or (sales["units_sold"] < 0).any():
        sys.exit("ERROR: sales_history.csv has non-numeric or negative units_sold")
    if sales.duplicated(["sku", "week_start"]).any():
        sys.exit("ERROR: duplicate (sku, week_start) rows in sales_history.csv")
    if lead["sku"].duplicated().any() or len(lead) != 40:
        sys.exit(f"ERROR: lead_times.csv should list 40 unique SKUs, found {len(lead)}")
    skus = lead["sku"].tolist()
    if not set(skus) <= set(sales["sku"]):
        sys.exit("ERROR: some lead_times SKUs have no sales history")
    sales = sales[sales["sku"].isin(skus)].sort_values(["sku", "week_start"]).reset_index(drop=True)
    diwali = fest.loc[fest["festival"].str.lower() == "diwali", "date"]
    diwali_weeks = [d - pd.Timedelta(days=d.dayofweek) for d in diwali]  # Monday of Diwali week
    want = {pd.Timestamp(w) for w in FORECAST_WEEKS}
    promo = promo[promo["sku"].isin(skus)]
    if set(promo["week_start"]) != want or promo.duplicated(["sku", "week_start"]).any() \
            or len(promo) != len(skus) * HORIZON:
        sys.exit("ERROR: promo_calendar.csv must cover each SKU for exactly the 4 forecast weeks")
    return sales, promo, lead, diwali_weeks, skus

# --------------------------------------------------------------- features ---

def diwali_k(weeks: pd.Series, diwali_weeks) -> np.ndarray:
    """Weeks until Diwali week (positive = before). NaN when outside [K_MIN, K_MAX]."""
    w = pd.to_datetime(weeks).to_numpy(dtype="datetime64[D]")
    best = np.full(len(w), np.nan)
    for d in diwali_weeks:
        k = (np.datetime64(d, "D") - w).astype(int) / 7.0
        ok = (k >= K_MIN) & (k <= K_MAX)
        best = np.where(ok & np.isnan(best), k, best)
    return best


def design(df: pd.DataFrame, t0: pd.Timestamp, diwali_weeks, cfg: Config, ks: list[int]):
    """Columns: promo, [trend], [sin, cos], one dummy per k in ks."""
    cols = [df["promo_flag"].to_numpy(float)]
    t = (pd.to_datetime(df["week_start"]) - t0).dt.days.to_numpy() / 7.0
    if cfg.trend:
        cols.append(t)
    if cfg.seasonal:
        ang = 2 * np.pi * pd.to_datetime(df["week_start"]).dt.dayofyear.to_numpy() / 365.25
        cols += [np.sin(ang), np.cos(ang)]
    k = diwali_k(df["week_start"], diwali_weeks)
    cols += [(k == kk).astype(float) for kk in ks]
    return np.column_stack(cols)

# ------------------------------------------------------------------ model ---

def forecast_origin(hist: pd.DataFrame, future: pd.DataFrame, diwali_weeks, cfg: Config) -> pd.DataFrame:
    """Forecast `future` (sku, week_start, promo_flag) using only `hist` (all weeks <= origin)."""
    hist = hist.sort_values(["sku", "week_start"])
    t0 = hist["week_start"].min()
    stats = hist.groupby("sku")["units_sold"].agg(n="size", zero=lambda x: (x == 0).mean())
    regular = stats.index[(stats["zero"] <= INTERMITTENT_ZERO_SHARE)]
    out = []

    # ---- regular SKUs: pooled log-linear model with SKU fixed effects
    reg = hist[hist["sku"].isin(regular) & (hist["units_sold"] > 0)].copy()
    reg["y"] = np.log(reg["units_sold"])
    fit_skus = stats.index[(stats["n"] >= MIN_FIT_WEEKS) & stats.index.isin(regular)]
    fit = reg[reg["sku"].isin(fit_skus)]
    kf = diwali_k(fit["week_start"], diwali_weeks)
    ks = [kk for kk in range(K_MIN, K_MAX + 1) if (kf == kk).sum() >= len(fit_skus) // 2]
    X = design(fit, t0, diwali_weeks, cfg, ks)
    y = fit["y"].to_numpy()
    grp = fit["sku"].to_numpy()
    Xd = X - pd.DataFrame(X).groupby(grp).transform("mean").to_numpy()   # within-SKU demeaning
    yd = y - pd.Series(y).groupby(grp).transform("mean").to_numpy()
    beta, *_ = np.linalg.lstsq(Xd, yd, rcond=None)

    # SKU level = EWMA of recent residuals (log scale) after removing global effects
    Xr = design(reg, t0, diwali_weeks, cfg, ks)
    reg["resid"] = reg["y"].to_numpy() - Xr @ beta
    smear = 1.0
    if cfg.smear:
        smear = float(np.mean(np.exp(reg["resid"] - reg.groupby("sku")["resid"].transform("mean"))))
    fut_reg = future[future["sku"].isin(regular)].copy()
    Xf = design(fut_reg, t0, diwali_weeks, cfg, ks)
    gf = Xf @ beta
    levels = {}
    for sku, g in reg.groupby("sku"):
        g = g.tail(cfg.window)
        age = (g["week_start"].max() - g["week_start"]).dt.days.to_numpy() / 7.0
        w = 0.5 ** (age / cfg.half_life)
        levels[sku] = float(np.sum(w * g["resid"].to_numpy()) / w.sum())
    fut_reg["forecast_units"] = np.exp(fut_reg["sku"].map(levels).to_numpy() + gf) * smear
    out.append(fut_reg[["sku", "week_start", "forecast_units"]])

    # ---- intermittent SKUs: recent plain average / median (never promoted in the data)
    inter = [s for s in stats.index if s not in set(regular)]
    fut_int = future[future["sku"].isin(inter)].copy()
    agg = hist[hist["sku"].isin(inter)].groupby("sku")["units_sold"].apply(
        lambda x: (x.tail(cfg.window).median() if cfg.intermittent == "median" else x.tail(cfg.window).mean()))
    fut_int["forecast_units"] = fut_int["sku"].map(agg).astype(float)
    out.append(fut_int[["sku", "week_start", "forecast_units"]])

    res = pd.concat(out, ignore_index=True)
    res["forecast_units"] = res["forecast_units"].clip(lower=0).fillna(0.0)
    return res

# -------------------------------------------------------------- baselines ---

def baselines(hist: pd.DataFrame, future: pd.DataFrame) -> pd.DataFrame:
    """last value, mean of last 4 weeks, same week last year (falls back to last value)."""
    rows = []
    for sku, g in hist.groupby("sku"):
        g = g.sort_values("week_start")
        s = g.set_index("week_start")["units_sold"]
        last, recent = float(s.iloc[-1]), float(s.tail(4).mean())
        for w in future.loc[future["sku"] == sku, "week_start"]:
            ly = s.get(w - pd.Timedelta(weeks=52), np.nan)
            rows.append((sku, w, last, recent, float(ly) if not np.isnan(ly) else np.nan))
    b = pd.DataFrame(rows, columns=["sku", "week_start", "last_value", "recent_avg", "same_week_ly"])
    b["same_week_ly_or_last"] = b["same_week_ly"].fillna(b["last_value"])
    return b

# --------------------------------------------------------------- backtest ---

def backtest(sales, diwali_weeks, cfg: Config, first_origin="2025-09-22") -> pd.DataFrame:
    """Rolling-origin backtest. For each origin, fit on weeks <= origin only and
    predict the next 4 weeks (using the *actual* promo flags of those weeks, which
    mirrors the known promo calendar at forecast time)."""
    weeks = sorted(sales["week_start"].unique())
    last_week = weeks[-1]
    rows = []
    for origin in [w for w in weeks if w >= pd.Timestamp(first_origin)]:
        targets = [origin + pd.Timedelta(weeks=h) for h in range(1, HORIZON + 1)]
        if targets[-1] > last_week:
            break
        hist = sales[sales["week_start"] <= origin]            # <- no future data in training
        fut = sales[sales["week_start"].isin(targets) & sales["sku"].isin(hist["sku"])][
            ["sku", "week_start", "promo_flag", "units_sold"]]
        pred = forecast_origin(hist, fut[["sku", "week_start", "promo_flag"]], diwali_weeks, cfg)
        base = baselines(hist, fut)
        m = fut.merge(pred, on=["sku", "week_start"]).merge(base, on=["sku", "week_start"])
        m["origin"] = origin
        rows.append(m)
    return pd.concat(rows, ignore_index=True)


def score(bt: pd.DataFrame) -> dict:
    out = {}
    for name in ["forecast_units", "last_value", "recent_avg", "same_week_ly_or_last"]:
        err = (bt[name] - bt["units_sold"]).abs()
        out[name] = {"WAPE": err.sum() / bt["units_sold"].sum(), "MAE": err.mean()}
    return out


def print_backtest(bt, label):
    print(f"\n[{label}] origins {bt['origin'].min().date()}..{bt['origin'].max().date()}, "
          f"{bt['origin'].nunique()} origins, {len(bt)} sku-week predictions")
    for name, m in score(bt).items():
        print(f"  {name:22s} WAPE {m['WAPE']:.4f}   MAE {m['MAE']:.3f}")

# ----------------------------------------------------------------- output ---

def validate_output(path: Path, skus: list[str]) -> None:
    df = pd.read_csv(path)
    assert list(df.columns) == ["sku", "week_start", "forecast_units"], f"bad columns {list(df.columns)}"
    assert len(df) == 160, f"expected 160 rows, got {len(df)}"
    assert set(df["sku"]) == set(skus) and len(set(df["sku"])) == 40, "SKU set mismatch"
    assert set(df["week_start"]) == set(FORECAST_WEEKS), "forecast weeks mismatch"
    assert not df.duplicated(["sku", "week_start"]).any(), "duplicate (sku, week_start)"
    assert (df.groupby("sku").size() == HORIZON).all(), "each SKU needs 4 rows"
    v = pd.to_numeric(df["forecast_units"], errors="raise")
    assert np.isfinite(v).all() and (v >= 0).all(), "forecast_units must be finite and >= 0"
    print(f"Validation OK: {path.relative_to(ROOT)} - {len(df)} rows, 40 SKUs x 4 weeks, "
          f"min {v.min():.2f}, max {v.max():.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument("--backtest-only", action="store_true")
    args = ap.parse_args()

    sales, promo, lead, diwali_weeks, skus = load_inputs()
    cfg = DEFAULT
    if not args.skip_backtest:
        bt = backtest(sales, diwali_weeks, cfg)
        print_backtest(bt, "all origins")
        k = diwali_k(bt["week_start"], diwali_weeks)
        print_backtest(bt[~np.isnan(k)], "targets inside Diwali window")
        print_backtest(bt[np.isnan(k)], "targets outside Diwali window")
    if args.backtest_only:
        return

    future = promo.rename(columns={"promo_flag": "promo_flag"})[["sku", "week_start", "promo_flag"]]
    fc = forecast_origin(sales, future, diwali_weeks, cfg)
    fc["week_start"] = fc["week_start"].dt.strftime("%Y-%m-%d")
    fc["forecast_units"] = fc["forecast_units"].round(2)
    fc["_o"] = fc["sku"].map({s: i for i, s in enumerate(skus)})
    fc = fc.sort_values(["_o", "week_start"]).drop(columns="_o")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fc.to_csv(OUT, index=False, columns=["sku", "week_start", "forecast_units"])
    validate_output(OUT, skus)


if __name__ == "__main__":
    main()
