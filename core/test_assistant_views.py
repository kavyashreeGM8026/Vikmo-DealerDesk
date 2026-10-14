"""Assistant endpoint error shapes and session binding. No LLM and no network:
`run_assistant` is always replaced by a mock."""
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from core import assistant_views
from core.services.assistant.sessions import SessionStore
from core.tests import assert_error_shape, make_dealer

URL = "/api/assistant/chat/"
RUN = "core.assistant_views.run_assistant"


def fake_result(reply="Hello."):
    return {"reply": reply, "tool_calls": [], "tool_results": [], "history": []}


@override_settings(GEMINI_API_KEY="test-key")
class AssistantErrorShapeTests(APITestCase):
    def setUp(self):
        assistant_views.session_store.clear()
        self.dealer = make_dealer()

    def tearDown(self):
        assistant_views.session_store.clear()

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
        self.assertEqual(assistant_views.session_store.dealer_id("s1"), self.dealer.id)

    def test_llm_failure_is_502_and_does_not_store_history(self):
        with mock.patch(RUN, side_effect=RuntimeError("provider exploded: secret-detail")):
            response = self.chat()
        error = assert_error_shape(self, response, 502, "ASSISTANT_UNAVAILABLE")
        self.assertNotIn("secret-detail", error["message"])
        self.assertIsNone(assistant_views.session_store.dealer_id("s1"))  # no trace left

    def test_success_response_shape_is_unchanged(self):
        with mock.patch(RUN, return_value=fake_result("Hi there.")):
            response = self.chat()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"reply": "Hi there.", "cited_skus": [], "tool_calls": [], "actions": []},
        )


@override_settings(GEMINI_API_KEY="test-key")
class AssistantSessionBehaviourTests(APITestCase):
    def setUp(self):
        self.store = SessionStore(max_sessions=50, lock_timeout=0.05)
        patcher = mock.patch.object(assistant_views, "session_store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dealer = make_dealer()

    def chat(self, session_id="s1", message="hi"):
        body = {"session_id": session_id, "dealer_id": self.dealer.id, "message": message}
        return self.client.post(URL, body, format="json")

    def test_history_is_passed_to_the_next_turn_of_the_same_session_only(self):
        seen = []

        def fake_run(message, context, history):
            seen.append(list(history))
            return {**fake_result(), "history": list(history) + [f"turn:{message}"]}

        with mock.patch(RUN, side_effect=fake_run):
            self.chat("a", "one")
            self.chat("a", "two")
            self.chat("b", "other")
        self.assertEqual(seen[0], [])
        self.assertEqual(seen[1], ["turn:one"])
        self.assertEqual(seen[2], [])  # a different session never sees session a's memory

    def test_llm_runs_while_the_global_lock_is_free(self):
        held = []

        def fake_run(message, context, history):
            held.append(self.store._lock.locked())
            return fake_result()

        with mock.patch(RUN, side_effect=fake_run):
            self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(held, [False])

    def test_overlapping_request_for_the_same_session_gets_429(self):
        with self.store.open("s1", self.dealer.id):  # simulates a request still in progress
            with mock.patch(RUN) as run:
                response = self.chat("s1")
        assert_error_shape(self, response, 429, "ASSISTANT_BUSY")
        run.assert_not_called()

    def test_a_busy_session_does_not_block_another_session(self):
        with self.store.open("s1", self.dealer.id):
            with mock.patch(RUN, return_value=fake_result()):
                self.assertEqual(self.chat("s2").status_code, 200)

    def test_history_stored_after_a_turn_is_trimmed_safely(self):
        long_history = [f"m{i}" for i in range(100)]
        with mock.patch(RUN, return_value={**fake_result(), "history": long_history}):
            with mock.patch.object(assistant_views, "trim_history", return_value=["kept"]) as trim:
                self.chat()
        trim.assert_called_once_with(long_history, assistant_views.MAX_HISTORY_MESSAGES)
        self.assertEqual(self.store.history("s1"), ["kept"])
