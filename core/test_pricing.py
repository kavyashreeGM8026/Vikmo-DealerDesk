"""Pure pricing tests: no database needed."""
from decimal import Decimal

from django.test import SimpleTestCase

from core.services.pricing import price_line, price_order

D = Decimal   


class PriceLineTests(SimpleTestCase):
    def test_contract_example(self):
        line = price_line(D("1459.50"), 3)
        self.assertEqual(line.line_subtotal, D("4378.50"))
        self.assertEqual(line.line_tax, D("788.13"))
        self.assertEqual(line.line_total, D("5166.63"))

    def test_tax_rounds_half_up_not_half_even(self):
        # 0.25 x 18% = 0.045 exactly -> 0.05 (half-even would give 0.04)
        self.assertEqual(price_line(D("0.25"), 1).line_tax, D("0.05"))
        # 10.25 x 18% = 1.845 exactly -> 1.85 (half-even would give 1.84)
        self.assertEqual(price_line(D("10.25"), 1).line_tax, D("1.85"))
        # 1.50 x 18% = 0.27 exactly; 0.05 x 18% = 0.009 -> 0.01; 0.02 x 18% = 0.0036 -> 0.00
        self.assertEqual(price_line(D("1.50"), 1).line_tax, D("0.27"))
        self.assertEqual(price_line(D("0.05"), 1).line_tax, D("0.01"))
        self.assertEqual(price_line(D("0.02"), 1).line_tax, D("0.00"))

    def test_line_total_is_subtotal_plus_tax(self):
        line = price_line(D("333.33"), 7)
        self.assertEqual(line.line_total, line.line_subtotal + line.line_tax)

    def test_results_always_have_exactly_two_decimals(self):
        line = price_line(D("100"), 2)
        for value in (line.unit_price, line.line_subtotal, line.line_tax, line.line_total):
            self.assertIsInstance(value, Decimal)
            self.assertEqual(value.as_tuple().exponent, -2)
        self.assertEqual(str(line.line_subtotal), "200.00")
        self.assertEqual(str(line.line_tax), "36.00")

    def test_large_amounts_are_exact(self):
        line = price_line(D("9999999999.99"), 2_147_483_647)
        self.assertEqual(line.line_subtotal, D("9999999999.99") * 2_147_483_647)

    def test_floats_and_bad_quantities_are_rejected(self):
        with self.assertRaises(TypeError):
            price_line(1459.5, 3)
        with self.assertRaises(TypeError):
            price_line(D("1.00"), 1.0)
        with self.assertRaises(TypeError):
            price_line(D("1.00"), True)
        with self.assertRaises(ValueError):
            price_line(D("1.00"), 0)
        with self.assertRaises(ValueError):
            price_line(D("1.00"), -3)
        with self.assertRaises(ValueError):
            price_line(D("-1.00"), 1)
        with self.assertRaises(ValueError):
            price_line(D("NaN"), 1)


class PriceOrderTests(SimpleTestCase):
    def test_totals_are_sums_of_lines(self):
        result = price_order([(D("1459.50"), 3), (D("349.00"), 2)])
        self.assertEqual(result.subtotal, D("4378.50") + D("698.00"))
        self.assertEqual(result.tax_total, D("788.13") + D("125.64"))
        self.assertEqual(result.grand_total, result.subtotal + result.tax_total)

    def test_tax_is_rounded_per_line_not_on_the_order_total(self):
        # Two lines of 0.25: each tax 0.045 -> 0.05, so tax_total 0.10.
        # Rounding the order total instead (0.50 x 18% = 0.09) would give 0.09.
        result = price_order([(D("0.25"), 1), (D("0.25"), 1)])
        self.assertEqual([line.line_tax for line in result.lines], [D("0.05"), D("0.05")])
        self.assertEqual(result.subtotal, D("0.50"))
        self.assertEqual(result.tax_total, D("0.10"))
        self.assertEqual(result.grand_total, D("0.60"))

    def test_lines_keep_input_order(self):
        result = price_order([(D("5.00"), 1), (D("1.00"), 9)])
        self.assertEqual([line.quantity for line in result.lines], [1, 9])

    def test_empty_order_is_zero(self):
        result = price_order([])
        self.assertEqual((result.subtotal, result.tax_total, result.grand_total), (D("0.00"),) * 3)
