"""Stage 2e: GET /api/forecast/{sku}/ and GET /api/reorder-suggestions/ (contract section 10)."""
import csv
import math
import statistics
from decimal import Decimal
from fractions import Fraction

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from core.models import Product
from core.services import reorder
from core.tests import assert_error_shape, make_product

D = Decimal
ENTRY_KEYS = {"sku", "current_stock", "lead_time_weeks", "lead_time_demand",
              "safety_stock", "reorder_point", "reorder_qty"}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def csv_forecast():
    out = {}
    for r in read_csv(settings.BASE_DIR / "ml" / "forecast.csv"):
        out.setdefault(r["sku"], []).append((r["week_start"], r["forecast_units"]))
    return {k: sorted(v) for k, v in out.items()}


def lead_times():
    return {r["sku"]: int(r["lead_time_weeks"]) for r in read_csv(settings.DATA_DIR / "lead_times.csv")}


def recent_units(sku):
    rows = sorted((r for r in read_csv(settings.DATA_DIR / "sales_history.csv") if r["sku"] == sku),
                  key=lambda r: r["week_start"])
    return [int(r["units_sold"]) for r in rows][-12:]


def reference(sku, weeks, stock):
    """Independent re-computation straight from the CSV text, using statistics.stdev."""
    demand = sum((D(u) for _w, u in csv_forecast()[sku][:weeks]), D(0))
    hist = recent_units(sku)
    sigma = statistics.stdev(hist) if len(hist) > 1 else 0.0
    ss = math.ceil(1.65 * sigma * math.sqrt(weeks))
    point = demand + ss
    return demand, ss, point, max(0, math.ceil(point - stock))


class SafetyStockMathTests(SimpleTestCase):
    def test_uses_sample_not_population_std(self):
        # [10, 20]: sample variance 50 -> 1.65*sqrt(50) = 11.67 -> 12 (population would give 7)
        self.assertEqual(reorder.safety_stock([Fraction(10), Fraction(20)], 1), 12)
        self.assertEqual(reorder.safety_stock([Fraction(10), Fraction(20)], 4), 24)  # x sqrt(4)

    def test_exact_integer_result_is_not_bumped_up(self):
        # sigma is exactly 20, so 1.65*20*sqrt(L) is exactly 33 (L=1) and 66 (L=4)
        h = [Fraction(80), Fraction(100), Fraction(120)]
        self.assertEqual(reorder.safety_stock(h, 1), 33)
        self.assertEqual(reorder.safety_stock(h, 4), 66)

    def test_only_most_recent_12_weeks_are_used(self):
        old_noise = [Fraction(v) for v in (0, 1000) * 5]
        flat = [Fraction(50)] * 12
        self.assertEqual(reorder.safety_stock(old_noise + flat, 2), 0)

    def test_fewer_than_12_weeks_uses_all_of_them(self):
        h = [Fraction(v) for v in (5, 9, 4, 12)]
        ref = math.ceil(1.65 * statistics.stdev([5, 9, 4, 12]) * math.sqrt(3))
        self.assertEqual(reorder.safety_stock(h, 3), ref)

    def test_sparse_history_is_safe(self):
        self.assertEqual(reorder.safety_stock([], 3), 0)
        self.assertEqual(reorder.safety_stock([Fraction(40)], 3), 0)

    def test_lead_time_demand_uses_first_L_weeks_unrounded(self):
        fc = [("2026-09-28", D("10.10")), ("2026-10-05", D("19.90")),
              ("2026-10-12", D("30.00")), ("2026-10-19", D("40.55"))]
        self.assertEqual(reorder.lead_time_demand(fc, 1), D("10.10"))
        self.assertEqual(reorder.lead_time_demand(fc, 2), D("30.00"))
        self.assertEqual(reorder.lead_time_demand(fc, 4), D("100.55"))

    def test_reorder_qty_formula(self):
        fc = [("w1", D("10.10")), ("w2", D("19.90")), ("w3", D("1")), ("w4", D("1"))]
        flat = [Fraction(7)] * 12  # sigma 0 -> safety stock 0
        # point = 30.00 exactly: stock 10 -> 20 (float noise must not give 21)
        self.assertEqual(reorder.reorder_entry("X", 2, fc, flat, 10)["reorder_qty"], 20)
        e = reorder.reorder_entry("X", 2, fc, flat, 10)
        self.assertEqual((e["lead_time_demand"], e["safety_stock"], e["reorder_point"]), (30.0, 0, 30.0))
        self.assertEqual(reorder.reorder_entry("X", 2, fc, flat, 29)["reorder_qty"], 1)
        self.assertEqual(reorder.reorder_entry("X", 2, fc, flat, 30)["reorder_qty"], 0)
        self.assertEqual(reorder.reorder_entry("X", 2, fc, flat, 500)["reorder_qty"], 0)  # never negative

    def test_reorder_qty_rounds_up_fractions(self):
        fc = [("w1", D("30.01"))] * 4
        e = reorder.reorder_entry("X", 1, fc, [], 30)  # missing history -> sigma 0
        self.assertEqual((e["safety_stock"], e["reorder_qty"]), (0, 1))


class ForecastApiTests(APITestCase):
    def test_returns_four_chronological_records_matching_csv(self):
        expected = csv_forecast()
        for sku in lead_times():
            with self.subTest(sku=sku):
                r = self.client.get(f"/api/forecast/{sku}/")
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertEqual(set(body), {"sku", "forecast"})
                self.assertEqual(body["sku"], sku)
                self.assertEqual(len(body["forecast"]), 4)
                self.assertEqual([f["week_start"] for f in body["forecast"]],
                                 ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"])
                self.assertEqual([f["forecast_units"] for f in body["forecast"]],
                                 [float(u) for _w, u in expected[sku]])

    def test_unknown_sku_is_404(self):
        assert_error_shape(self, self.client.get("/api/forecast/NOPE-0000/"), 404, "NOT_FOUND")

    def test_catalogue_sku_that_is_not_a_forecast_sku_is_404(self):
        in_forecast = set(lead_times())
        other = next(r["sku"] for r in read_csv(settings.DATA_DIR / "catalogue.csv")
                     if r["sku"] not in in_forecast)
        make_product(sku=other)
        assert_error_shape(self, self.client.get(f"/api/forecast/{other}/"), 404, "NOT_FOUND")


class ReorderSuggestionsApiTests(APITestCase):
    def results(self):
        r = self.client.get("/api/reorder-suggestions/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body), {"results"})  # not paginated
        return {e["sku"]: e for e in body["results"]}

    def test_forty_entries_with_all_fields(self):
        res = self.results()
        self.assertEqual(set(res), set(lead_times()))
        self.assertEqual(len(res), 40)
        for e in res.values():
            self.assertEqual(set(e), ENTRY_KEYS)

    def test_missing_products_count_as_zero_stock(self):
        self.assertEqual(Product.objects.count(), 0)
        for e in self.results().values():
            self.assertEqual(e["current_stock"], 0)

    def test_every_entry_matches_independent_formula(self):
        stocks = {}
        for i, sku in enumerate(sorted(lead_times())):
            stocks[sku] = (i * 7) % 90
            make_product(sku=sku, stock=stocks[sku])
        for sku, e in self.results().items():
            with self.subTest(sku=sku):
                demand, ss, point, qty = reference(sku, lead_times()[sku], stocks[sku])
                self.assertEqual(e["current_stock"], stocks[sku])
                self.assertEqual(e["lead_time_weeks"], lead_times()[sku])
                self.assertEqual(e["lead_time_demand"], float(demand))
                self.assertEqual(e["safety_stock"], ss)
                self.assertEqual(e["reorder_point"], float(point))
                self.assertEqual(e["reorder_qty"], qty)
                self.assertGreaterEqual(e["reorder_qty"], 0)

    def test_lead_time_demand_is_sum_of_first_L_weeks(self):
        fc = csv_forecast()
        for sku, e in self.results().items():
            L = lead_times()[sku]
            self.assertEqual(e["lead_time_demand"], float(sum((D(u) for _w, u in fc[sku][:L]), D(0))))

    def test_current_stock_is_live(self):
        sku = next(iter(lead_times()))
        make_product(sku=sku, stock=3)
        first = self.results()[sku]
        self.assertEqual(first["current_stock"], 3)
        Product.objects.filter(sku=sku).update(stock=100000)
        second = self.results()[sku]
        self.assertEqual(second["current_stock"], 100000)
        self.assertEqual(second["reorder_qty"], 0)
        self.assertGreater(first["reorder_qty"], 0)

    def test_endpoint_does_not_change_stock(self):
        sku = next(iter(lead_times()))
        make_product(sku=sku, stock=12)
        self.results()
        self.assertEqual(Product.objects.get(pk=sku).stock, 12)
        self.assertEqual(self.client.post("/api/reorder-suggestions/", {}).status_code, 405)
