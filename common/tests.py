"""Tests for the shared plumbing: the error shape, pagination and strict integer input.

They use a tiny throw-away URL config (at the bottom of this file) instead of real
endpoints, and SimpleTestCase, so they never touch the database.
"""
from django.http import Http404, HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import DuplicateError, NotFoundError
from common.fields import StrictIntegerField
from common.pagination import StandardPagination


# ---- demo views used only by these tests --------------------------------------------------
class DemoSerializer(serializers.Serializer):
    name = serializers.CharField()
    quantity = StrictIntegerField(min_value=0, max_value=100)


class ListView(APIView):
    def get(self, request):
        paginator = StandardPagination()
        page = paginator.paginate_queryset(list(range(1, 46)), request, view=self)  # 45 items
        return paginator.get_paginated_response(page)


class CreateView(APIView):
    def post(self, request):
        serializer = DemoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=201)


class DuplicateView(APIView):
    def get(self, request):
        raise DuplicateError("Product with SKU 'X' already exists.")


class NotFoundView(APIView):
    def get(self, request):
        raise NotFoundError("Product 'X' not found.")


class Http404View(APIView):
    def get(self, request):
        raise Http404


class CrashView(APIView):
    def get(self, request):
        raise RuntimeError("boom")


def plain_crash(request):  # a normal Django view that escapes DRF entirely
    raise RuntimeError("boom")


urlpatterns = [
    path("demo/list/", ListView.as_view()),
    path("demo/create/", CreateView.as_view()),
    path("demo/duplicate/", DuplicateView.as_view()),
    path("demo/notfound/", NotFoundView.as_view()),
    path("demo/http404/", Http404View.as_view()),
    path("demo/crash/", CrashView.as_view()),
    path("demo/plain-crash/", plain_crash),
]
handler404 = "common.views.not_found"
handler500 = "common.views.server_error"


def assert_error_shape(test, response, status, code):
    test.assertEqual(response.status_code, status)
    body = response.json()
    test.assertEqual(set(body), {"error"})
    test.assertEqual(set(body["error"]), {"code", "message", "details"})
    test.assertEqual(body["error"]["code"], code)
    test.assertIsInstance(body["error"]["details"], list)
    return body["error"]


@override_settings(ROOT_URLCONF="common.tests", DEBUG=False)
class ErrorShapeTests(SimpleTestCase):
    def test_validation_error_lists_each_bad_field(self):
        res = self.client.post("/demo/create/", {"name": "", "quantity": 2.5}, content_type="application/json")
        error = assert_error_shape(self, res, 400, "VALIDATION_ERROR")
        self.assertEqual({d["field"] for d in error["details"]}, {"name", "quantity"})

    def test_valid_input_passes(self):
        res = self.client.post("/demo/create/", {"name": "pad", "quantity": 3}, content_type="application/json")
        self.assertEqual((res.status_code, res.json()), (201, {"name": "pad", "quantity": 3}))

    def test_malformed_json_is_400(self):
        res = self.client.post("/demo/create/", "{bad json", content_type="application/json")
        assert_error_shape(self, res, 400, "VALIDATION_ERROR")

    def test_body_that_is_not_an_object_is_400(self):
        res = self.client.post("/demo/create/", [1, 2], content_type="application/json")
        assert_error_shape(self, res, 400, "VALIDATION_ERROR")

    def test_duplicate_is_409(self):
        error = assert_error_shape(self, self.client.get("/demo/duplicate/"), 409, "DUPLICATE")
        self.assertIn("already exists", error["message"])

    def test_not_found_error_and_http404_are_404(self):
        assert_error_shape(self, self.client.get("/demo/notfound/"), 404, "NOT_FOUND")
        assert_error_shape(self, self.client.get("/demo/http404/"), 404, "NOT_FOUND")

    def test_unknown_url_is_json_404_not_an_html_page(self):
        res = self.client.get("/demo/does-not-exist/")
        assert_error_shape(self, res, 404, "NOT_FOUND")
        self.assertEqual(res["Content-Type"], "application/json")

    def test_wrong_method_is_405(self):
        assert_error_shape(self, self.client.post("/demo/list/", {}, content_type="application/json"),
                           405, "METHOD_NOT_ALLOWED")

    def test_wrong_content_type_is_415(self):
        res = self.client.post("/demo/create/", "name=x", content_type="application/x-www-form-urlencoded")
        assert_error_shape(self, res, 415, "UNSUPPORTED_MEDIA_TYPE")

    def test_crash_in_a_view_is_500_json_and_is_logged(self):
        with self.assertLogs("common.exceptions", level="ERROR") as logs:
            res = self.client.get("/demo/crash/")
        error = assert_error_shape(self, res, 500, "INTERNAL_ERROR")
        self.assertNotIn("boom", error["message"])  # no internals leaked to the client
        self.assertIn("boom", "".join(logs.output))  # but the traceback is in our logs

    def test_crash_outside_drf_uses_handler500(self):
        self.client.raise_request_exception = False
        assert_error_shape(self, self.client.get("/demo/plain-crash/"), 500, "INTERNAL_ERROR")


@override_settings(ROOT_URLCONF="common.tests", DEBUG=False)
class PaginationTests(SimpleTestCase):
    def get(self, query=""):
        return self.client.get("/demo/list/" + query).json()

    def test_shape_and_defaults(self):
        body = self.get()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual((body["count"], len(body["results"])), (45, 20))
        self.assertIsNone(body["previous"])
        self.assertIsNotNone(body["next"])

    def test_last_page_is_short_and_has_no_next(self):
        body = self.get("?page=3")
        self.assertEqual(len(body["results"]), 5)
        self.assertIsNone(body["next"])
        self.assertIsNotNone(body["previous"])

    def test_page_size_is_respected_and_capped_at_100(self):
        self.assertEqual(len(self.get("?page_size=7")["results"]), 7)
        self.assertEqual(len(self.get("?page_size=1000")["results"]), 45)  # cap is 100, only 45 exist

    def test_garbage_page_size_falls_back_to_default(self):
        self.assertEqual(len(self.get("?page_size=abc")["results"]), 20)

    def test_page_out_of_range_is_json_404(self):
        assert_error_shape(self, self.client.get("/demo/list/?page=99"), 404, "NOT_FOUND")


class StrictIntegerFieldTests(SimpleTestCase):
    def check(self, value, **kwargs):
        field = StrictIntegerField(**kwargs)
        try:
            return field.run_validation(value)
        except serializers.ValidationError:
            return "REJECTED"

    def test_accepts_real_integers(self):
        self.assertEqual(self.check(5), 5)
        self.assertEqual(self.check(0, min_value=0), 0)

    def test_rejects_everything_that_is_not_a_json_integer(self):
        for bad in [True, False, 2.5, 25.0, "25", "abc", None, [1], {"a": 1}]:
            self.assertEqual(self.check(bad), "REJECTED", repr(bad))

    def test_enforces_bounds(self):
        self.assertEqual(self.check(-1, min_value=0), "REJECTED")
        self.assertEqual(self.check(101, max_value=100), "REJECTED")
        self.assertEqual(self.check(100, max_value=100), 100)


class HealthStillWorksTests(SimpleTestCase):
    def test_health_endpoint(self):
        res = self.client.get("/api/health/")
        self.assertEqual((res.status_code, res.json()), (200, {"status": "ok"}))
