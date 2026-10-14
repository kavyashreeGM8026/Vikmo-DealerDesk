"""Assistant endpoint error shapes and session binding. No LLM and no network:
`run_assistant` is always replaced by a mock."""
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from core import assistant_views
from core.tests import assert_error_shape, make_dealer

URL = "/api/assistant/chat/"
RUN = "core.assistant_views.run_assistant"


def fake_result(reply="Hello."):
    return {"reply": reply, "tool_calls": [], "tool_results": [], "history": []}


@override_settings(GEMINI_API_KEY="test-key")
class AssistantErrorShapeTests(APITestCase):
    def setUp(self):
        assistant_views._sessions.clear()
        self.dealer = make_dealer()

    def tearDown(self):
        assistant_views._sessions.clear()

    def chat(self, **overrides):
        body = {"session_id": "s1", "dealer_id": self.dealer.id, "message": "hi"}
        body.update(overrides)
        return self.client.post(URL, body, format="json")

    def test_unknown_dealer_is_400_validation_error(self):
        with mock.patch(RUN) as run:
            response = self.chat(dealer_id=999999)
        error = assert_error_shape(self, response, 400, "VALIDATION_ERROR")
        self.assertEqual(error["details"][0]["field"], "dealer_id")
        run.assert_not_called()

    def test_malformed_body_uses_the_standard_shape(self):
        response = self.client.post(URL, {"session_id": "s1", "dealer_id": self.dealer.id}, format="json")
        assert_error_shape(self, response, 400, "VALIDATION_ERROR")

    @override_settings(GEMINI_API_KEY="")
    def test_assistant_not_configured_is_503_in_the_standard_shape(self):
        with mock.patch(RUN) as run:
            response = self.chat()
        assert_error_shape(self, response, 503, "ASSISTANT_NOT_CONFIGURED")
        run.assert_not_called()

    def test_session_cannot_be_reused_by_another_dealer(self):
        other = make_dealer(dealer_code="DLR-008", email="other@example.in")
        with mock.patch(RUN, return_value=fake_result()) as run:
            self.assertEqual(self.chat().status_code, 200)
            response = self.chat(dealer_id=other.id)
        error = assert_error_shape(self, response, 400, "VALIDATION_ERROR")
        self.assertEqual(error["details"][0]["field"], "session_id")
        self.assertEqual(run.call_count, 1)  # the second request never reached the model
        self.assertEqual(assistant_views._sessions["s1"]["dealer_id"], self.dealer.id)

    def test_llm_failure_is_502_and_does_not_store_history(self):
        with mock.patch(RUN, side_effect=RuntimeError("provider exploded: secret-detail")):
            response = self.chat()
        error = assert_error_shape(self, response, 502, "ASSISTANT_UNAVAILABLE")
        self.assertNotIn("secret-detail", error["message"])
        self.assertNotIn("s1", assistant_views._sessions)

    def test_success_response_shape_is_unchanged(self):
        with mock.patch(RUN, return_value=fake_result("Hi there.")):
            response = self.chat()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"reply": "Hi there.", "cited_skus": [], "tool_calls": [], "actions": []},
        )
