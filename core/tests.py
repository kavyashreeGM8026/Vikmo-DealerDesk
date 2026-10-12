import csv
import re
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from core.models import Dealer, Product
from core.serializers import (
    DealerSerializer,
    InventoryUpdateSerializer,
    MoneyField,
    NonNegativeMoneyField,
    ProductCreateSerializer,
)

MONEY = re.compile(r"^-?\d+\.\d{2}$")


def assert_error_shape(test, response, status, code):
    test.assertEqual(response.status_code, status)
    body = response.json()
    test.assertEqual(set(body), {"error"})
    test.assertEqual(set(body["error"]), {"code", "message", "details"})
    test.assertEqual(body["error"]["code"], code)
    test.assertIsInstance(body["error"]["details"], list)
    return body["error"]


def make_product(**overrides):
    data = {
        "sku": "BRK-1044",
        "name": "Brake Pad Set - Bajaj Pulsar 150",
        "category": "Brakes",
        "brand": "Endurance",
        "vehicle_fitment": "Bajaj Pulsar 150",
        "price": Decimal("1459.50"),
        "stock": 136,
        "description": "Direct-fit brake pads.",
    }
    data.update(overrides)
    return Product.objects.create(**data)


class ProductSerializerValidationTests(SimpleTestCase):
    """These checks do not touch the database, so they can run without Postgres."""

    def test_valid_price_is_accepted(self):
        self.assertEqual(str(MoneyField().run_validation("1459.50")), "1459.50")

    def test_invalid_prices_are_rejected(self):
        from rest_framework.exceptions import ValidationError

        for price in ["0", "-1.00", "10.123", "abc"]:
            with self.assertRaises(ValidationError, msg=repr(price)):
                MoneyField().run_validation(price)

    def test_create_serializer_does_not_query_for_sku_uniqueness(self):
        serializer = ProductCreateSerializer(
            data={
                "sku": "X-1",
                "name": "N",
                "category": "C",
                "brand": "B",
                "vehicle_fitment": "V",
                "price": "10.00",
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_inventory_quantity_must_be_a_json_integer_at_least_zero(self):
        self.assertTrue(InventoryUpdateSerializer(data={"quantity": 0}).is_valid())
        self.assertTrue(InventoryUpdateSerializer(data={"quantity": 25, "reason": "count"}).is_valid())
        for bad in [-1, 2.5, 25.0, "25", True, None]:
            self.assertFalse(InventoryUpdateSerializer(data={"quantity": bad}).is_valid(), repr(bad))


class ProductApiTests(APITestCase):
    def setUp(self):
        self.brake = make_product()
        self.filter_kit = make_product(
            sku="FIL-1012",
            name="Oil Filter - KTM Duke 390",
            category="Filters",
            brand="Hiflo",
            vehicle_fitment="KTM Duke 390",
            price=Decimal("349.00"),
            stock=40,
            description="Oil filter.",
        )
        self.chain = make_product(
            sku="CHN-1001",
            name="Chain Sprocket Kit - Bajaj Pulsar 150",
            category="Drivetrain",
            brand="RK",
            vehicle_fitment="Bajaj Pulsar 150",
            price=Decimal("2990.00"),
            stock=102,
            description="Chain kit.",
        )

    def test_list_shape_pagination_and_money_format(self):
        res = self.client.get("/api/products/?page_size=2")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["results"]), 2)
        self.assertIsNotNone(body["next"])
        self.assertIsNone(body["previous"])
        product = body["results"][0]
        for key in ("sku", "name", "category", "brand", "vehicle_fitment", "price", "stock", "description"):
            self.assertIn(key, product)
        self.assertIsInstance(product["price"], str)
        self.assertRegex(product["price"], MONEY)
        self.assertIsInstance(product["stock"], int)

    def test_list_is_ordered_by_sku(self):
        skus = [row["sku"] for row in self.client.get("/api/products/").json()["results"]]
        self.assertEqual(skus, sorted(skus))

    def test_search_matches_sku_name_or_brand_case_insensitive(self):
        by_sku = self.client.get("/api/products/?search=brk-1044").json()
        self.assertEqual(by_sku["count"], 1)
        self.assertEqual(by_sku["results"][0]["sku"], "BRK-1044")

        by_name = self.client.get("/api/products/?search=oil filter").json()
        self.assertEqual(by_name["count"], 1)
        self.assertEqual(by_name["results"][0]["sku"], "FIL-1012")

        by_brand = self.client.get("/api/products/?search=endurance").json()
        self.assertEqual(by_brand["count"], 1)
        self.assertEqual(by_brand["results"][0]["sku"], "BRK-1044")

    def test_category_filter_is_exact(self):
        body = self.client.get("/api/products/?category=Brakes").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["sku"], "BRK-1044")
        empty = self.client.get("/api/products/?category=brakes").json()
        self.assertEqual(empty["count"], 0)

    def test_vehicle_filter_is_case_insensitive_substring(self):
        body = self.client.get("/api/products/?vehicle=pulsar").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual({row["sku"] for row in body["results"]}, {"BRK-1044", "CHN-1001"})

    def test_search_and_vehicle_can_combine(self):
        # search covers sku, name AND brand, so "RK" also hits the SKU "BRK-1044".
        # "Brake" appears only in the brake pad's name, so the combination is unambiguous.
        body = self.client.get("/api/products/?search=Brake&vehicle=Pulsar").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["sku"], "BRK-1044")

        # vehicle really narrows the result: a brake pad does not fit a Duke
        body = self.client.get("/api/products/?search=Brake&vehicle=Duke").json()
        self.assertEqual(body["count"], 0)

    def test_detail_200_and_missing_sku_404(self):
        res = self.client.get("/api/products/BRK-1044/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["sku"], "BRK-1044")
        self.assertEqual(res.json()["price"], "1459.50")
        self.assertEqual(res.json()["stock"], 136)
        assert_error_shape(self, self.client.get("/api/products/NOPE-0000/"), 404, "NOT_FOUND")

    def test_create_product_starts_at_stock_zero(self):
        payload = {
            "sku": "BDY-9001",
            "name": "New Mirror",
            "category": "Body & Styling",
            "brand": "Zana",
            "vehicle_fitment": "Universal",
            "price": "123.45",
            "description": "A test part.",
        }
        res = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["sku"], "BDY-9001")
        self.assertEqual(body["stock"], 0)
        self.assertEqual(body["price"], "123.45")
        self.assertEqual(Product.objects.get(pk="BDY-9001").stock, 0)

    def test_create_ignores_client_supplied_stock(self):
        payload = {
            "sku": "BDY-9002",
            "name": "New Mirror",
            "category": "Body & Styling",
            "brand": "Zana",
            "vehicle_fitment": "Universal",
            "price": "10.00",
            "stock": 99,
        }
        res = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["stock"], 0)

    def test_duplicate_sku_is_409(self):
        payload = {
            "sku": "BRK-1044",
            "name": "Duplicate",
            "category": "Brakes",
            "brand": "X",
            "vehicle_fitment": "Universal",
            "price": "10.00",
        }
        error = assert_error_shape(
            self, self.client.post("/api/products/", payload, format="json"), 409, "DUPLICATE"
        )
        self.assertIn("BRK-1044", error["message"])

    def test_invalid_price_is_400(self):
        base = {
            "sku": "NEW-1",
            "name": "Part",
            "category": "Brakes",
            "brand": "X",
            "vehicle_fitment": "Universal",
        }
        for price in ["0", "-1.00", "10.123", "abc"]:
            payload = {**base, "sku": f"NEW-{price}", "price": price}
            assert_error_shape(
                self, self.client.post("/api/products/", payload, format="json"), 400, "VALIDATION_ERROR"
            )

    def test_patch_updates_price_but_not_sku(self):
        res = self.client.patch(
            "/api/products/BRK-1044/",
            {"price": "1500.00", "sku": "HACK-1"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["sku"], "BRK-1044")
        self.assertEqual(body["price"], "1500.00")
        self.assertFalse(Product.objects.filter(pk="HACK-1").exists())

    def test_put_on_product_detail_is_405(self):
        assert_error_shape(
            self,
            self.client.put("/api/products/BRK-1044/", {"name": "x"}, format="json"),
            405,
            "METHOD_NOT_ALLOWED",
        )

    def test_page_out_of_range_is_json_404(self):
        assert_error_shape(self, self.client.get("/api/products/?page=99"), 404, "NOT_FOUND")


class InventoryApiTests(APITestCase):
    def setUp(self):
        self.product = make_product()

    def test_put_sets_quantity_and_product_stock(self):
        res = self.client.put(
            "/api/inventory/BRK-1044/",
            {"quantity": 25, "reason": "stock count correction"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"sku": "BRK-1044", "quantity": 25})
        self.assertEqual(self.client.get("/api/products/BRK-1044/").json()["stock"], 25)

    def test_quantity_zero_is_allowed(self):
        res = self.client.put("/api/inventory/BRK-1044/", {"quantity": 0}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["quantity"], 0)

    def test_unknown_sku_is_404(self):
        assert_error_shape(
            self,
            self.client.put("/api/inventory/NOPE-0000/", {"quantity": 1}, format="json"),
            404,
            "NOT_FOUND",
        )

    def test_invalid_quantity_is_400(self):
        for bad in [-1, 2.5, 25.0, "25", True, None]:
            assert_error_shape(
                self,
                self.client.put("/api/inventory/BRK-1044/", {"quantity": bad}, format="json"),
                400,
                "VALIDATION_ERROR",
            )


class SeedDataTests(APITestCase):
    def test_seed_is_duplicate_safe_and_preserves_existing_stock(self):
        make_product(sku="BDY-1001", stock=7, price=Decimal("1.00"), name="Old")
        out = StringIO()
        call_command("seed_data", stdout=out)
        first = out.getvalue()
        self.assertIn("created", first)

        bdy = Product.objects.get(pk="BDY-1001")
        self.assertEqual(bdy.stock, 7)
        self.assertEqual(bdy.name, "Old")

        total_after_first = Product.objects.count()
        self.assertGreaterEqual(total_after_first, 600)

        out2 = StringIO()
        call_command("seed_data", stdout=out2)
        self.assertEqual(Product.objects.count(), total_after_first)
        self.assertEqual(Product.objects.get(pk="BDY-1001").stock, 7)


# ====================================================================================
# Stage 2c: Dealers
# ====================================================================================

DEALER_FIELDS = ("id", "dealer_code", "name", "city", "phone", "email", "gstin", "credit_limit")


def make_dealer(**overrides):
    data = {
        "dealer_code": "DLR-007",
        "name": "Wheels & Deals",
        "city": "Hyderabad",
        "phone": "9214159864",
        "email": "orders@wheelsdeals.in",
        "gstin": "29AABH2853G1Z8",
        "credit_limit": Decimal("250000.00"),
    }
    data.update(overrides)
    return Dealer.objects.create(**data)


class DealerModelTests(APITestCase):
    def test_create_dealer_stores_all_fields(self):
        dealer = make_dealer()
        dealer.refresh_from_db()
        self.assertIsNotNone(dealer.id)
        self.assertEqual(dealer.dealer_code, "DLR-007")
        self.assertEqual(dealer.name, "Wheels & Deals")
        self.assertEqual(dealer.city, "Hyderabad")
        self.assertEqual(dealer.phone, "9214159864")
        self.assertEqual(dealer.email, "orders@wheelsdeals.in")
        self.assertEqual(dealer.gstin, "29AABH2853G1Z8")
        self.assertEqual(dealer.credit_limit, Decimal("250000.00"))
        self.assertIsInstance(dealer.credit_limit, Decimal)
        self.assertEqual(str(dealer), "DLR-007")

    def test_optional_fields_have_defaults(self):
        dealer = Dealer.objects.create(dealer_code="DLR-900", name="Bare Minimum")
        dealer.refresh_from_db()
        self.assertEqual((dealer.city, dealer.phone, dealer.email, dealer.gstin), ("", "", "", ""))
        self.assertEqual(dealer.credit_limit, Decimal("0.00"))

    def test_dealer_code_is_unique_in_the_database(self):
        make_dealer()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_dealer(name="Other")
        self.assertEqual(Dealer.objects.count(), 1)

    def test_credit_limit_is_exact_decimal(self):
        dealer = make_dealer(credit_limit=Decimal("0.10") + Decimal("0.20"))
        dealer.refresh_from_db()
        self.assertEqual(dealer.credit_limit, Decimal("0.30"))


class DealerSerializerValidationTests(SimpleTestCase):
    """No database needed."""

    def test_credit_limit_accepts_zero_and_two_decimals(self):
        field = NonNegativeMoneyField()
        self.assertEqual(str(field.run_validation("0")), "0.00")
        self.assertEqual(str(field.run_validation("100000")), "100000.00")
        self.assertEqual(str(field.run_validation("1234.5")), "1234.50")

    def test_credit_limit_rejects_bad_values(self):
        from rest_framework.exceptions import ValidationError

        for bad in ["-1", "-0.01", "10.123", "abc", "NaN", "Infinity", True]:
            with self.assertRaises(ValidationError, msg=repr(bad)):
                NonNegativeMoneyField().run_validation(bad)

    def test_serializer_does_not_query_for_code_uniqueness(self):
        serializer = DealerSerializer(data={"dealer_code": "X-1", "name": "N"})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_required_fields(self):
        serializer = DealerSerializer(data={})
        self.assertFalse(serializer.is_valid())
        self.assertEqual(set(serializer.errors), {"dealer_code", "name"})


class DealerApiTests(APITestCase):
    def setUp(self):
        self.dealer = make_dealer()

    # ---- list ----------------------------------------------------------------------
    def test_list_shape_and_fields(self):
        res = self.client.get("/api/dealers/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 1)
        self.assertIsNone(body["next"])
        self.assertIsNone(body["previous"])
        row = body["results"][0]
        for key in DEALER_FIELDS:
            self.assertIn(key, row)
        self.assertEqual(row["id"], self.dealer.id)
        self.assertEqual(row["dealer_code"], "DLR-007")
        self.assertEqual(row["credit_limit"], "250000.00")

    def test_list_pagination_default_and_page_size(self):
        Dealer.objects.bulk_create(
            [Dealer(dealer_code=f"P-{i:03d}", name=f"Dealer {i}") for i in range(24)]
        )  # 25 dealers in total with the one from setUp
        first = self.client.get("/api/dealers/").json()
        self.assertEqual(first["count"], 25)
        self.assertEqual(len(first["results"]), 20)  # default page size
        self.assertIsNotNone(first["next"])
        self.assertIsNone(first["previous"])

        second = self.client.get("/api/dealers/?page=2").json()
        self.assertEqual(len(second["results"]), 5)
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])

        ids = [r["id"] for r in first["results"]] + [r["id"] for r in second["results"]]
        self.assertEqual(len(set(ids)), 25)  # no overlap, nothing missing
        self.assertEqual(ids, sorted(ids))  # stable ordering by id

        small = self.client.get("/api/dealers/?page_size=2").json()
        self.assertEqual(len(small["results"]), 2)
        self.assertEqual(small["count"], 25)

    def test_list_page_size_is_capped_at_100(self):
        Dealer.objects.bulk_create(
            [Dealer(dealer_code=f"Q-{i:03d}", name=f"Dealer {i}") for i in range(110)]
        )
        body = self.client.get("/api/dealers/?page_size=1000").json()
        self.assertEqual(len(body["results"]), 100)
        self.assertEqual(body["count"], 111)

    def test_list_page_out_of_range_is_json_404(self):
        assert_error_shape(self, self.client.get("/api/dealers/?page=99"), 404, "NOT_FOUND")

    # ---- create --------------------------------------------------------------------
    def test_create_with_all_fields_returns_201(self):
        payload = {
            "dealer_code": "DLR-100",
            "name": "New Dealer",
            "city": "Bengaluru",
            "phone": "9876543210",
            "email": "dealer@example.com",
            "gstin": "29ABCDE1234F1Z5",
            "credit_limit": "100000.00",
        }
        res = self.client.post("/api/dealers/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(set(body), set(DEALER_FIELDS))
        self.assertIsInstance(body["id"], int)
        for key, value in payload.items():
            self.assertEqual(body[key], value)
        self.assertTrue(Dealer.objects.filter(dealer_code="DLR-100").exists())

    def test_create_with_only_required_fields(self):
        res = self.client.post(
            "/api/dealers/", {"dealer_code": "DLR-101", "name": "Minimal"}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["dealer_code"], "DLR-101")
        self.assertEqual(body["city"], "")
        self.assertEqual(body["credit_limit"], "0.00")

    def test_create_accepts_null_for_optional_fields(self):
        payload = {
            "dealer_code": "DLR-102",
            "name": "Nulls",
            "city": None,
            "phone": None,
            "email": None,
            "gstin": None,
            "credit_limit": None,
        }
        res = self.client.post("/api/dealers/", payload, format="json")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual((body["city"], body["email"]), ("", ""))
        self.assertEqual(body["credit_limit"], "0.00")

    def test_create_trims_whitespace(self):
        res = self.client.post(
            "/api/dealers/", {"dealer_code": "  DLR-103 ", "name": " Trim Me "}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["dealer_code"], "DLR-103")
        self.assertEqual(res.json()["name"], "Trim Me")

    def test_missing_required_fields_is_400(self):
        for payload in [
            {},
            {"name": "No Code"},
            {"dealer_code": "DLR-200"},
            {"dealer_code": "", "name": "Blank Code"},
            {"dealer_code": "DLR-201", "name": ""},
            {"dealer_code": "   ", "name": "Spaces Code"},
            {"dealer_code": None, "name": "Null Code"},
            {"dealer_code": "DLR-202", "name": None},
        ]:
            res = self.client.post("/api/dealers/", payload, format="json")
            assert_error_shape(self, res, 400, "VALIDATION_ERROR")
        self.assertEqual(Dealer.objects.count(), 1)  # nothing was created

    def test_missing_field_is_named_in_error_details(self):
        res = self.client.post("/api/dealers/", {"name": "No Code"}, format="json")
        error = assert_error_shape(self, res, 400, "VALIDATION_ERROR")
        self.assertEqual([d["field"] for d in error["details"]], ["dealer_code"])

    def test_invalid_optional_values_are_400(self):
        base = {"dealer_code": "DLR-300", "name": "Bad Input"}
        for extra in [
            {"credit_limit": "-1.00"},
            {"credit_limit": "10.123"},
            {"credit_limit": "abc"},
            {"credit_limit": "99999999999.00"},  # too many digits for the column
            {"email": "not-an-email"},
            {"dealer_code": "X" * 65},
            {"phone": "9" * 33},
        ]:
            res = self.client.post("/api/dealers/", {**base, **extra}, format="json")
            assert_error_shape(self, res, 400, "VALIDATION_ERROR")
        self.assertFalse(Dealer.objects.filter(dealer_code="DLR-300").exists())

    def test_malformed_json_is_400(self):
        res = self.client.post("/api/dealers/", "{not json", content_type="application/json")
        assert_error_shape(self, res, 400, "VALIDATION_ERROR")

    def test_duplicate_dealer_code_is_409_and_changes_nothing(self):
        res = self.client.post(
            "/api/dealers/",
            {"dealer_code": "DLR-007", "name": "Impostor", "credit_limit": "1.00"},
            format="json",
        )
        error = assert_error_shape(self, res, 409, "DUPLICATE")
        self.assertIn("DLR-007", error["message"])
        self.assertEqual(Dealer.objects.count(), 1)
        self.dealer.refresh_from_db()
        self.assertEqual(self.dealer.name, "Wheels & Deals")
        self.assertEqual(self.dealer.credit_limit, Decimal("250000.00"))

    def test_duplicate_code_with_missing_name_is_still_a_validation_error(self):
        # Validation runs before the duplicate check.
        res = self.client.post("/api/dealers/", {"dealer_code": "DLR-007"}, format="json")
        assert_error_shape(self, res, 400, "VALIDATION_ERROR")

    # ---- retrieve ------------------------------------------------------------------
    def test_get_existing_dealer(self):
        res = self.client.get(f"/api/dealers/{self.dealer.id}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(),
            {
                "id": self.dealer.id,
                "dealer_code": "DLR-007",
                "name": "Wheels & Deals",
                "city": "Hyderabad",
                "phone": "9214159864",
                "email": "orders@wheelsdeals.in",
                "gstin": "29AABH2853G1Z8",
                "credit_limit": "250000.00",
            },
        )

    def test_get_nonexistent_dealer_is_404(self):
        missing = self.dealer.id + 1000
        assert_error_shape(self, self.client.get(f"/api/dealers/{missing}/"), 404, "NOT_FOUND")

    def test_get_dealer_with_bad_id_is_404_not_500(self):
        for bad in ["abc", "DLR-007", "-1", "1.5", "99999999999999999999999"]:
            assert_error_shape(self, self.client.get(f"/api/dealers/{bad}/"), 404, "NOT_FOUND")

    def test_created_dealer_can_be_fetched(self):
        created = self.client.post(
            "/api/dealers/", {"dealer_code": "DLR-400", "name": "Round Trip"}, format="json"
        ).json()
        fetched = self.client.get(f"/api/dealers/{created['id']}/").json()
        self.assertEqual(created, fetched)

    def test_detail_only_allows_get(self):
        url = f"/api/dealers/{self.dealer.id}/"
        for method in (self.client.put, self.client.patch, self.client.post, self.client.delete):
            assert_error_shape(self, method(url, {"name": "x"}, format="json"), 405, "METHOD_NOT_ALLOWED")

    # ---- money format --------------------------------------------------------------
    def test_credit_limit_is_always_a_string_with_two_decimals(self):
        for index, (sent, expected) in enumerate([
            ("100000.00", "100000.00"),
            (100000, "100000.00"),
            ("5000", "5000.00"),
            ("123.4", "123.40"),
            (0, "0.00"),
            (1500.5, "1500.50"),
        ]):
            res = self.client.post(
                "/api/dealers/",
                {
                    "dealer_code": f"M-{index}-{expected}",
                    "name": "Money",
                    "credit_limit": sent,
                },
                format="json",
            )
            self.assertEqual(res.status_code, 201, res.content)
            value = res.json()["credit_limit"]
            self.assertIsInstance(value, str)
            self.assertEqual(value, expected)
            self.assertRegex(value, MONEY)

        for row in self.client.get("/api/dealers/?page_size=100").json()["results"]:
            self.assertIsInstance(row["credit_limit"], str)
            self.assertRegex(row["credit_limit"], MONEY)

    def test_credit_limit_stored_value_is_exact(self):
        res = self.client.post(
            "/api/dealers/",
            {"dealer_code": "DLR-500", "name": "Exact", "credit_limit": "100000.00"},
            format="json",
        )
        self.assertEqual(Dealer.objects.get(pk=res.json()["id"]).credit_limit, Decimal("100000.00"))


class DealerSeedTests(APITestCase):
    """seed_data now loads data/dealers.csv as well as data/catalogue.csv."""

    @staticmethod
    def csv_rows():
        with (settings.DATA_DIR / "dealers.csv").open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def seed(self):
        out, err = StringIO(), StringIO()
        call_command("seed_data", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_seed_loads_every_dealer_from_csv(self):
        rows = self.csv_rows()
        self.assertEqual(len(rows), 25)
        out, err = self.seed()
        self.assertIn("Dealers: 25 created", out)
        self.assertEqual(err, "")
        self.assertEqual(Dealer.objects.count(), 25)
        self.assertEqual(
            set(Dealer.objects.values_list("dealer_code", flat=True)),
            {row["dealer_code"] for row in rows},
        )

    def test_seed_maps_every_csv_column(self):
        self.seed()
        for row in self.csv_rows():
            dealer = Dealer.objects.get(dealer_code=row["dealer_code"])
            self.assertEqual(dealer.name, row["name"])
            self.assertEqual(dealer.city, row["city"])
            self.assertEqual(dealer.phone, row["phone"])
            self.assertEqual(dealer.email, row["email"])
            self.assertEqual(dealer.gstin, row["gstin"])
            self.assertEqual(dealer.credit_limit, Decimal(row["credit_limit"]))

    def test_seeded_dealers_are_served_by_the_api(self):
        self.seed()
        body = self.client.get("/api/dealers/?page_size=100").json()
        self.assertEqual(body["count"], 25)
        first = body["results"][0]
        self.assertEqual(first["dealer_code"], "DLR-001")
        self.assertEqual(first["name"], "ABC Motors")
        self.assertRegex(first["credit_limit"], MONEY)

    def test_running_seed_twice_creates_no_duplicates(self):
        self.seed()
        dealers, products = Dealer.objects.count(), Product.objects.count()
        out, _ = self.seed()
        self.assertEqual(Dealer.objects.count(), dealers)
        self.assertEqual(Product.objects.count(), products)
        self.assertIn("Dealers: 0 created, 25 already present", out)
        self.assertEqual(
            Dealer.objects.values("dealer_code").distinct().count(), Dealer.objects.count()
        )

    def test_seed_does_not_overwrite_or_delete_existing_dealers(self):
        mine = make_dealer(dealer_code="DLR-001", name="My Edited Name", credit_limit=Decimal("1.00"))
        extra = make_dealer(dealer_code="DLR-999", name="Not In CSV")
        self.seed()
        mine.refresh_from_db()
        self.assertEqual(mine.name, "My Edited Name")
        self.assertEqual(mine.credit_limit, Decimal("1.00"))
        self.assertTrue(Dealer.objects.filter(pk=extra.pk).exists())
        self.assertEqual(Dealer.objects.count(), 26)  # 25 from CSV (one pre-existing) + extra

    def test_seed_preserves_catalogue_behaviour_and_live_data(self):
        make_product(sku="BDY-1001", stock=7, price=Decimal("1.00"), name="Old")
        self.seed()
        self.assertGreaterEqual(Product.objects.count(), 600)
        bdy = Product.objects.get(pk="BDY-1001")
        self.assertEqual((bdy.stock, bdy.price, bdy.name), (7, Decimal("1.00"), "Old"))

        # Live stock/price changes survive a second run (dealers seeding touches no products).
        other = Product.objects.exclude(pk="BDY-1001").first()
        Product.objects.filter(pk=other.pk).update(stock=3, price=Decimal("9.99"))
        self.seed()
        other.refresh_from_db()
        self.assertEqual((other.stock, other.price), (3, Decimal("9.99")))


class DealerSeedMalformedCsvTests(APITestCase):
    """Uses a temporary data/ folder so bad files never touch the real CSVs."""

    HEADER = "dealer_code,name,city,phone,email,gstin,credit_limit\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copy(settings.DATA_DIR / "catalogue.csv", self.tmp / "catalogue.csv")

    def write_dealers(self, text, encoding="utf-8"):
        (self.tmp / "dealers.csv").write_bytes(text.encode(encoding))

    def seed(self):
        out, err = StringIO(), StringIO()
        with override_settings(DATA_DIR=self.tmp):
            call_command("seed_data", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_bad_rows_are_skipped_with_a_warning_and_good_rows_still_load(self):
        self.write_dealers(
            self.HEADER
            + "OK-1,Good One,Pune,9000000001,a@b.in,GST1,1000.50\n"
            + ",No Code,Pune,9000000002,c@d.in,GST2,10.00\n"
            + "BAD-2,,Pune,9000000003,e@f.in,GST3,10.00\n"
            + "BAD-3,Bad Limit,Pune,9000000004,g@h.in,GST4,lots\n"
            + "BAD-4,Neg Limit,Pune,9000000005,i@j.in,GST5,-5.00\n"
            + "BAD-5,Too Precise,Pune,9000000006,k@l.in,GST6,1.234\n"
            + "BAD-6,Bad Email,Pune,9000000007,nope,GST7,1.00\n"
            + "OK-2,Short Row\n"
            + "\n"
        )
        out, err = self.seed()
        self.assertEqual(
            set(Dealer.objects.values_list("dealer_code", flat=True)), {"OK-1", "OK-2"}
        )
        self.assertEqual(Dealer.objects.get(dealer_code="OK-1").credit_limit, Decimal("1000.50"))
        short = Dealer.objects.get(dealer_code="OK-2")
        self.assertEqual((short.city, short.credit_limit), ("", Decimal("0.00")))
        self.assertIn("6 invalid rows skipped", out)
        self.assertEqual(err.count("skipped:"), 6)
        # The catalogue was still seeded.
        self.assertGreaterEqual(Product.objects.count(), 600)

    def test_duplicate_codes_inside_the_csv_create_one_dealer(self):
        self.write_dealers(
            self.HEADER
            + "DUP-1,First,Pune,1,a@b.in,G,10.00\n"
            + "DUP-1,Second,Goa,2,c@d.in,H,20.00\n"
        )
        self.seed()
        self.assertEqual(Dealer.objects.filter(dealer_code="DUP-1").count(), 1)
        self.assertEqual(Dealer.objects.get(dealer_code="DUP-1").name, "First")

    def test_bom_blank_credit_limit_and_padded_values_are_handled(self):
        self.write_dealers(
            "\ufeff" + self.HEADER + " BOM-1 , Padded Name , Goa , 1 , a@b.in , G ,\n",
        )
        self.seed()
        dealer = Dealer.objects.get(dealer_code="BOM-1")
        self.assertEqual((dealer.name, dealer.city), ("Padded Name", "Goa"))
        self.assertEqual(dealer.credit_limit, Decimal("0.00"))

    def test_missing_column_is_a_clear_error_and_loads_no_dealers(self):
        self.write_dealers("dealer_code,name\nA-1,Only Two Columns\n")
        with self.assertRaises(CommandError) as ctx:
            self.seed()
        self.assertIn("credit_limit", str(ctx.exception))
        self.assertEqual(Dealer.objects.count(), 0)

    def test_empty_file_is_a_clear_error(self):
        self.write_dealers("")
        with self.assertRaises(CommandError):
            self.seed()

    def test_missing_dealers_file_is_a_clear_error_and_seeds_nothing(self):
        with self.assertRaises(CommandError) as ctx:
            self.seed()
        self.assertIn("dealers.csv", str(ctx.exception))
        self.assertEqual(Product.objects.count(), 0)  # nothing half-seeded

    def test_missing_catalogue_file_keeps_the_original_error(self):
        (self.tmp / "catalogue.csv").unlink()
        self.write_dealers(self.HEADER)
        with self.assertRaises(CommandError) as ctx:
            self.seed()
        self.assertIn("Catalogue file not found", str(ctx.exception))
