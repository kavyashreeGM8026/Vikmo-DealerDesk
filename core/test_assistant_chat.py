from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from core.models import Dealer
from core.tests import assert_error_shape, make_dealer


class AssistantChatErrorTests(APITestCase):
    url = "/api/assistant/chat/"

    def setUp(self):
        self.dealer = make_dealer()
        self.payload = {
            "session_id": "assistant-error-test",
            "dealer_id": self.dealer.pk,
            "message": "Find brake pads",
        }

    def test_unknown_dealer_uses_validation_error_shape(self):
        self.payload["dealer_id"] = 999999
        response = self.client.post(self.url, self.payload, format="json")
        assert_error_shape(self, response, 400, "VALIDATION_ERROR")

    @override_settings(GEMINI_API_KEY="")
    def test_unconfigured_assistant_uses_service_unavailable_shape(self):
        response = self.client.post(self.url, self.payload, format="json")
        assert_error_shape(self, response, 503, "ASSISTANT_NOT_CONFIGURED")

    @override_settings(GEMINI_API_KEY="test-key")
    @mock.patch(
        "core.assistant_views.run_assistant",
        side_effect=RuntimeError("simulated upstream failure"),
    )
    def test_upstream_failure_uses_upstream_error_shape(self, mocked_run):
        payload = {**self.payload, "session_id": "upstream-failure-test"}
        response = self.client.post(self.url, payload, format="json")
        assert_error_shape(self, response, 502, "ASSISTANT_UNAVAILABLE")
        mocked_run.assert_called_once()

    @override_settings(GEMINI_API_KEY="test-key")
    @mock.patch("core.assistant_views.run_assistant")
    def test_session_cannot_be_reused_for_another_dealer(self, mocked_run):
        second_dealer = make_dealer(
            dealer_code="CHAT-SECOND",
            name="Second Chat Dealer",
        )
        # Establish the session's dealer binding without calling the LLM.
        from core.assistant_views import session_store

        with session_store.open("assistant-error-test", self.dealer.pk) as session:
            session.commit([])

        self.payload["dealer_id"] = second_dealer.pk
        response = self.client.post(self.url, self.payload, format="json")
        assert_error_shape(self, response, 400, "VALIDATION_ERROR")
        mocked_run.assert_not_called()



