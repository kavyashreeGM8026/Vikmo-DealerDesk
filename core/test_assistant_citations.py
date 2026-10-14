"""`cited_skus` must list only the SKUs the reply actually mentions, and only SKUs that
came from trusted tool results (and exist). No LLM, no network: `run_assistant` is mocked."""
from unittest import mock

from django.test import override_settings
from google.genai import types
from rest_framework.test import APITestCase

from core import assistant_views
from core.services.assistant.citations import extract_cited_skus
from core.tests import make_dealer, make_product

URL = "/api/assistant/chat/"
RUN = "core.assistant_views.run_assistant"


def search_result(*skus):
    return {"name": "search_parts", "result": {"ok": True, "products": [{"sku": s} for s in skus]}}


def stock_result(sku, found=True):
    return {"name": "check_stock", "result": {"ok": True, "found": found, "sku": sku}}


def order_result(*skus):
    return {
        "name": "create_draft_order",
        "result": {"ok": True, "order": {"order_number": "ORD-1", "items": [{"sku": s} for s in skus]}},
    }


class CitationFixtureMixin:
    def make_catalogue(self):
        for sku in ("BRK-1044", "BRK-2048", "CHN-1001", "FIL-1012"):
            make_product(sku=sku, name=f"Part {sku}")


class ExtractCitedSkusTests(CitationFixtureMixin, APITestCase):
    def setUp(self):
        self.make_catalogue()

    def cited(self, reply, *tool_results, history=None):
        return extract_cited_skus(reply, list(tool_results), history)

    def test_only_the_mentioned_sku_is_cited_not_every_search_result(self):
        reply = "The compatible part is BRK-1044."
        self.assertEqual(self.cited(reply, search_result("BRK-1044", "BRK-2048")), ["BRK-1044"])

    def test_multiple_mentions_come_back_in_reply_order(self):
        reply = "Take CHN-1001 first, then BRK-1044."
        self.assertEqual(
            self.cited(reply, search_result("BRK-1044", "CHN-1001", "BRK-2048")),
            ["CHN-1001", "BRK-1044"],
        )

    def test_duplicate_mentions_are_listed_once(self):
        reply = "BRK-1044 is in stock. Yes, BRK-1044 ships today (brk-1044)."
        self.assertEqual(self.cited(reply, search_result("BRK-1044")), ["BRK-1044"])

    def test_no_sku_in_the_reply_gives_an_empty_list(self):
        self.assertEqual(self.cited("Which vehicle is it for?", search_result("BRK-1044")), [])
        self.assertEqual(self.cited("Hello!"), [])

    def test_sku_like_text_without_a_trusted_tool_result_is_ignored(self):
        reply = "Try ZZZ-9999, or BRK-1044."
        self.assertEqual(self.cited(reply, search_result("BRK-1044")), ["BRK-1044"])

    def test_a_real_catalogue_sku_that_no_tool_returned_is_not_cited(self):
        # FIL-1012 exists in the database, but nothing trusted vouches for it in this turn.
        self.assertEqual(self.cited("Also see FIL-1012.", search_result("BRK-1044")), [])

    def test_a_sku_that_only_contains_a_trusted_sku_is_not_a_mention(self):
        for reply in ("Code BRK-10445.", "Code XBRK-1044.", "Code BRK-1044-X.", "Code BRK-1044_2."):
            self.assertEqual(self.cited(reply, search_result("BRK-1044")), [], reply)

    def test_punctuation_and_markdown_around_a_sku_still_count(self):
        reply = "**BRK-1044**, (CHN-1001); `FIL-1012`."
        self.assertEqual(
            self.cited(reply, search_result("BRK-1044", "CHN-1001", "FIL-1012")),
            ["BRK-1044", "CHN-1001", "FIL-1012"],
        )

    def test_check_stock_sku_counts_only_when_found(self):
        self.assertEqual(self.cited("BRK-1044 has stock.", stock_result("BRK-1044")), ["BRK-1044"])
        self.assertEqual(self.cited("BRK-1044 has stock.", stock_result("BRK-1044", found=False)), [])

    def test_draft_order_skus_count(self):
        reply = "Draft created for CHN-1001 and BRK-2048."
        self.assertEqual(self.cited(reply, order_result("BRK-2048", "CHN-1001")), ["CHN-1001", "BRK-2048"])

    def test_skus_from_several_tool_calls_are_combined(self):
        reply = "BRK-1044 and FIL-1012 are available."
        self.assertEqual(
            self.cited(reply, search_result("BRK-1044"), stock_result("FIL-1012")),
            ["BRK-1044", "FIL-1012"],
        )

    def test_a_trusted_sku_missing_from_the_database_is_dropped(self):
        reply = "BRK-1044 and GONE-0001."
        self.assertEqual(self.cited(reply, search_result("BRK-1044", "GONE-0001")), ["BRK-1044"])

    def test_failed_unknown_and_malformed_tool_results_are_ignored_safely(self):
        reply = "BRK-1044 and BRK-2048."
        junk = [
            None,
            "text",
            {},
            {"name": "search_parts"},
            {"name": "search_parts", "result": "oops"},
            {"name": "search_parts", "result": {"products": "oops"}},
            {"name": "search_parts", "result": {"products": [None, 5, {"sku": None}, {"sku": 7}, {"sku": ""}, {}]}},
            {"name": "check_stock", "result": {"found": True}},
            {"name": "check_stock", "result": {"found": True, "sku": ["BRK-1044"]}},
            {"name": "create_draft_order", "result": {"order": "oops"}},
            {"name": "create_draft_order", "result": {"order": {"items": "oops"}}},
            {"name": "create_draft_order", "result": {"order": {"items": [None, {"sku": 3}]}}},
            {"name": "mystery_tool", "result": {"products": [{"sku": "BRK-1044"}]}},
            {"name": "search_parts", "result": {"ok": False, "error": "bad"}},
        ]
        self.assertEqual(extract_cited_skus(reply, junk), [])
        self.assertEqual(extract_cited_skus(reply, junk + [search_result("BRK-2048")]), ["BRK-2048"])
        self.assertEqual(extract_cited_skus(None, [search_result("BRK-1044")]), [])
        self.assertEqual(extract_cited_skus("BRK-1044", None), [])

    def test_skus_from_earlier_turns_in_the_history_are_trusted_too(self):
        history = [
            types.Content(role="user", parts=[types.Part.from_text(text="brake pads?")]),
            types.Content(role="model", parts=[types.Part.from_function_call(name="search_parts", args={})]),
            types.Content(
                role="user",
                parts=[types.Part.from_function_response(
                    name="search_parts",
                    response={"result": {"ok": True, "products": [{"sku": "BRK-1044"}, {"sku": "BRK-2048"}]}},
                )],
            ),
            types.Content(role="model", parts=[types.Part.from_text(text="BRK-1044 fits.")]),
        ]
        self.assertEqual(self.cited("As I said, BRK-1044.", history=history), ["BRK-1044"])
        self.assertEqual(self.cited("ZZZ-9999 only.", history=history), [])

    def test_malformed_history_is_ignored_safely(self):
        history = [None, "x", {"parts": "no"}, types.Content(role="user", parts=[types.Part.from_text(text="hi")])]
        self.assertEqual(self.cited("BRK-1044", search_result("BRK-1044"), history=history), ["BRK-1044"])


@override_settings(GEMINI_API_KEY="test-key")
class CitedSkusEndpointTests(CitationFixtureMixin, APITestCase):
    def setUp(self):
        assistant_views.session_store.clear()
        self.addCleanup(assistant_views.session_store.clear)
        self.make_catalogue()
        self.dealer = make_dealer()

    def chat(self, reply, tool_results, history=None, session_id="cite-1"):
        result = {"reply": reply, "tool_calls": [], "tool_results": tool_results, "history": history or []}
        with mock.patch(RUN, return_value=result):
            return self.client.post(
                URL,
                {"session_id": session_id, "dealer_id": self.dealer.id, "message": "hi"},
                format="json",
            )

    def test_endpoint_cites_only_the_sku_the_reply_mentions(self):
        response = self.chat("The compatible part is BRK-1044.", [search_result("BRK-1044", "BRK-2048")])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cited_skus"], ["BRK-1044"])

    def test_endpoint_cites_nothing_when_the_reply_mentions_no_sku(self):
        response = self.chat("Which model is your bike?", [search_result("BRK-1044", "BRK-2048")])
        self.assertEqual(response.json()["cited_skus"], [])

    def test_endpoint_ignores_untrusted_sku_like_text(self):
        response = self.chat("Maybe ZZZ-9999 or BRK-1044.", [search_result("BRK-1044")])
        self.assertEqual(response.json()["cited_skus"], ["BRK-1044"])

    def test_response_schema_is_unchanged(self):
        response = self.chat("Hi.", [])
        self.assertEqual(set(response.json()), {"reply", "cited_skus", "tool_calls", "actions"})
