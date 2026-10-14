"""Safe history trimming with real google-genai Content objects. No network."""
from django.test import SimpleTestCase
from google.genai import types

from core.services.assistant.history import is_turn_start, trim_history


def user_text(text):
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def model_text(text):
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def model_call(name="search_parts", **args):
    return types.Content(role="model", parts=[types.Part.from_function_call(name=name, args=args)])


def tool_response(name="search_parts"):
    return types.Content(
        role="user",
        parts=[types.Part.from_function_response(name=name, response={"result": {"ok": True}})],
    )


def turn(n, tool_rounds):
    messages = [user_text(f"question {n}")]
    for _ in range(tool_rounds):
        messages += [model_call(), tool_response()]
    messages.append(model_text(f"answer {n}"))
    return messages


def is_valid_history(contents):
    """The rules Gemini enforces: starts with a user text turn; every function_response
    directly follows a model function_call; ends with a model message."""
    if not contents or not is_turn_start(contents[0]):
        return False
    for i, content in enumerate(contents):
        has_response = any(p.function_response for p in content.parts)
        if has_response:
            prev = contents[i - 1] if i else None
            if prev is None or prev.role != "model" or not any(p.function_call for p in prev.parts):
                return False
    return contents[-1].role == "model"


def long_conversation(turns=16):
    history = []
    for n in range(turns):
        history += turn(n, tool_rounds=n % 4)  # turns of 2, 4, 6 and 8 messages
    return history


class TurnStartTests(SimpleTestCase):
    def test_only_plain_user_text_starts_a_turn(self):
        self.assertTrue(is_turn_start(user_text("hi")))
        self.assertFalse(is_turn_start(model_text("hi")))
        self.assertFalse(is_turn_start(model_call()))
        self.assertFalse(is_turn_start(tool_response()))

    def test_plain_dicts_are_understood_too(self):
        self.assertTrue(is_turn_start({"role": "user", "parts": [{"text": "hi"}]}))
        self.assertFalse(is_turn_start({"role": "user", "parts": [{"function_response": {"name": "x"}}]}))


class TrimHistoryTests(SimpleTestCase):
    def test_a_plain_slice_of_this_conversation_would_be_invalid(self):
        """Guards the test data: it must actually break the old `[-24:]` approach."""
        history = long_conversation()
        self.assertTrue(is_valid_history(history))
        self.assertFalse(is_valid_history(history[-24:]))

    def test_long_conversation_is_trimmed_to_a_valid_history(self):
        history = long_conversation()
        trimmed = trim_history(history, 24)
        self.assertLessEqual(len(trimmed), 24)
        self.assertTrue(is_valid_history(trimmed))
        self.assertIs(trimmed[-1], history[-1])  # newest reply kept
        self.assertTrue(trimmed[0].parts[0].text.startswith("question"))

    def test_every_possible_cut_point_gives_a_valid_history(self):
        history = long_conversation()
        for limit in range(1, len(history) + 1):
            trimmed = trim_history(history, limit)
            self.assertTrue(is_valid_history(trimmed), f"limit={limit}")
            self.assertIs(trimmed[-1], history[-1])

    def test_short_history_is_unchanged(self):
        history = turn(1, 1)
        self.assertEqual(trim_history(history, 24), history)

    def test_a_newest_turn_longer_than_the_limit_is_kept_whole(self):
        history = turn(0, 0) + turn(1, 6)  # newest turn has 14 messages
        trimmed = trim_history(history, 5)
        self.assertEqual(len(trimmed), 14)
        self.assertTrue(is_valid_history(trimmed))

    def test_input_list_is_not_modified(self):
        history = long_conversation()
        before = list(history)
        trim_history(history, 24)
        self.assertEqual(history, before)
