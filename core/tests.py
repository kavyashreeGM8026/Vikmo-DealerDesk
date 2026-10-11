import re
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from core.models import Product
from core.serializers import InventoryUpdateSerializer, MoneyField, ProductCreateSerializer

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
