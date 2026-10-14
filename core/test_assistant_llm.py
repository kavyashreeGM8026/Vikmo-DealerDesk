"""The Gemini loop in `llm.run_assistant`, with the SDK client mocked (no network, no API key)."""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from google.genai import types

from core.services.assistant import llm


class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} error")
        self.code = code


def text_response(text):
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], text=text)


def tool_response(name, args):
    part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    content = types.Content(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], text=None)


@override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="test-model")
class RunAssistantTests(SimpleTestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.generate = self.client.models.generate_content
        mock.patch.object(llm.genai, "Client", return_value=self.client).start()
        self.sleep = mock.patch.object(llm, "_sleep").start()
        self.addCleanup(mock.patch.stopall)

    def run_turn(self, message="hello"):
        return llm.run_assistant(message=message, context=mock.Mock(), history=[])

    def test_plain_reply(self):
        self.generate.side_effect = [text_response("Hi there")]
        result = self.run_turn()
        self.assertEqual(result["reply"], "Hi there")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(self.generate.call_count, 1)

    def test_transient_error_then_success_retries_once(self):
        self.generate.side_effect = [FakeAPIError(503), text_response("ok")]
        self.assertEqual(self.run_turn()["reply"], "ok")
        self.assertEqual(self.generate.call_count, 2)
        self.sleep.assert_called_once_with(1.0)

    def test_rate_limit_is_retried(self):
        self.generate.side_effect = [FakeAPIError(429), text_response("ok")]
        self.assertEqual(self.run_turn()["reply"], "ok")

    def test_exhausted_retries_raise(self):
        self.generate.side_effect = [FakeAPIError(503)] * 5
        with self.assertRaises(FakeAPIError):
            self.run_turn()
        self.assertEqual(self.generate.call_count, 3)

    def test_permanent_client_error_is_not_retried(self):
        self.generate.side_effect = [FakeAPIError(400)]
        with self.assertRaises(FakeAPIError):
            self.run_turn()
        self.assertEqual(self.generate.call_count, 1)
        self.sleep.assert_not_called()

    def test_out_of_time_budget_stops_before_calling_gemini(self):
        with mock.patch.object(llm, "TOTAL_BUDGET_SECONDS", 1):
            with self.assertRaises(RuntimeError):
                self.run_turn()
        self.generate.assert_not_called()

    def test_tool_call_round_is_recorded_and_executed_once_even_if_a_later_call_is_retried(self):
        self.generate.side_effect = [
            tool_response("check_stock", {"sku": "BRK-1044"}),
            FakeAPIError(503),
            text_response("BRK-1044 has 5 in stock."),
        ]
        with mock.patch.object(llm, "execute_tool", return_value={"ok": True, "found": True,
                                                                  "sku": "BRK-1044"}) as tool:
            result = self.run_turn("stock of BRK-1044?")
        tool.assert_called_once()
        self.assertEqual(result["tool_calls"], [{"name": "check_stock", "arguments": {"sku": "BRK-1044"}}])
        self.assertEqual(result["reply"], "BRK-1044 has 5 in stock.")

    def test_system_prompt_asks_for_a_vehicle_and_distrusts_old_figures(self):
        prompt = llm.SYSTEM_INSTRUCTION
        self.assertIn("clarifying question", prompt)
        self.assertIn("vehicle", prompt)
        self.assertIn("out of date", prompt)
        self.assertIn("untrusted", prompt)

    @override_settings(GEMINI_API_KEY="")
    def test_missing_key_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_turn()
