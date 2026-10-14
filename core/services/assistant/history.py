"""Safe trimming of the assistant's Gemini conversation history.

History is a list of `google.genai.types.Content` objects. One *turn* looks like:

    user  : plain text                      <- turn start
    model : function_call part(s)           \\  zero or more tool rounds
    user  : function_response part(s)       /
    model : plain text reply

Gemini rejects a history that starts with a model message, or that contains a
function_response without the function_call that precedes it. Slicing the list with
`history[-N:]` can cut a turn in half, so we only ever cut at a turn start.
"""


def _get(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _parts(content):
    return _get(content, "parts") or []


def is_turn_start(content):
    """True for a user message that carries plain text and no function_response."""
    if _get(content, "role") != "user":
        return False
    parts = _parts(content)
    if any(_get(part, "function_response") is not None for part in parts):
        return False
    return any(_get(part, "text") for part in parts)


def trim_history(contents, max_messages):
    """Return at most ~`max_messages` recent messages, starting at a turn start.

    Whole turns are dropped from the front. If the newest turn alone is longer than
    `max_messages` it is kept whole, because a valid history matters more than the limit.
    """
    contents = list(contents)
    if max_messages <= 0 or len(contents) <= max_messages:
        return contents

    start = len(contents) - max_messages
    for index in range(start, len(contents)):
        if is_turn_start(contents[index]):
            return contents[index:]

    for index in range(start - 1, -1, -1):  # newest turn is longer than the limit
        if is_turn_start(contents[index]):
            return contents[index:]

    return contents
