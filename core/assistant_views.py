
"""HTTP endpoint for the DealerDesk AI assistant."""

import logging
import threading
from collections import OrderedDict

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Dealer
from core.services.assistant.llm import run_assistant
from core.services.assistant.tools import ToolContext

logger = logging.getLogger(__name__)

MAX_SESSIONS = 500
MAX_HISTORY_MESSAGES = 24

# In-memory history, isolated by session ID. Suitable for a single app process.
_sessions = OrderedDict()
_sessions_lock = threading.RLock()


class AssistantChatSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=128, trim_whitespace=True)
    dealer_id = serializers.IntegerField(min_value=1)
    message = serializers.CharField(
        max_length=2000,
        trim_whitespace=True,
        allow_blank=False,
    )


class AssistantChatView(APIView):
    """POST /api/assistant/chat/."""

    def post(self, request):
        serializer = AssistantChatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session_id = serializer.validated_data["session_id"]
        dealer_id = serializer.validated_data["dealer_id"]
        message = serializer.validated_data["message"]

        try:
            dealer = Dealer.objects.get(pk=dealer_id)
        except Dealer.DoesNotExist:
            return Response(
                {"detail": "Dealer not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not getattr(settings, "GEMINI_API_KEY", ""):
            return Response(
                {"detail": "The assistant is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Keep a session bound to its original dealer and serialize turns within
        # a session so concurrent requests cannot corrupt its conversation order.
        with _sessions_lock:
            session = _sessions.get(session_id)

            if session and session["dealer_id"] != dealer_id:
                return Response(
                    {"detail": "This session belongs to a different dealer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            history = list(session["history"]) if session else []

            context = ToolContext(dealer=dealer)

            try:
                result = run_assistant(
                    message=message,
                    context=context,
                    history=history,
                )
            except Exception:
                logger.exception("Assistant chat failed for session %s", session_id)
                return Response(
                    {"detail": "The assistant could not complete the request. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            new_history = result["history"][-MAX_HISTORY_MESSAGES:]

            _sessions[session_id] = {
                "dealer_id": dealer_id,
                "history": new_history,
            }
            _sessions.move_to_end(session_id)

            while len(_sessions) > MAX_SESSIONS:
                _sessions.popitem(last=False)

        tool_calls = result.get("tool_calls", [])
        tool_results = result.get("tool_results", [])
        cited_skus = []

        def add_sku(value):
            if isinstance(value, str) and value and value not in cited_skus:
                cited_skus.append(value)

        for tool_result in tool_results:
            name = tool_result.get("name")
            result_data = tool_result.get("result", {})

            if not isinstance(result_data, dict):
                continue

            if name == "search_parts":
                for product in result_data.get("products", []):
                    if isinstance(product, dict):
                        add_sku(product.get("sku"))

            elif name == "check_stock":
                if result_data.get("found"):
                    add_sku(result_data.get("sku"))

            elif name == "create_draft_order":
                order = result_data.get("order", result_data)
                if isinstance(order, dict):
                    add_sku(order.get("sku"))
                    for item in order.get("items", []):
                        if isinstance(item, dict):
                            add_sku(item.get("sku"))

        actions = [
            {
                "type": "draft_order_created",
                "order_number": order_number,
            }
            for order_number in context.created_orders
        ]

        return Response(
            {
                "reply": result["reply"],
                "cited_skus": cited_skus,
                "tool_calls": tool_calls,
                "actions": actions,
            },
            status=status.HTTP_200_OK,
        )
