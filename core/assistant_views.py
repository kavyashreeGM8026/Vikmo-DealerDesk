
"""HTTP endpoint for the DealerDesk AI assistant."""

import logging

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import error_response
from core.models import Dealer
from core.services.assistant.history import trim_history
from core.services.assistant.llm import run_assistant
from core.services.assistant.sessions import (
    SessionBusy,
    SessionDealerMismatch,
    SessionStore,
)
from core.services.assistant.tools import ToolContext

logger = logging.getLogger(__name__)

MAX_SESSIONS = 500
MAX_HISTORY_MESSAGES = 24

# In-memory history, isolated by session ID. Suitable for a single app process.
session_store = SessionStore(max_sessions=MAX_SESSIONS)


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
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                "VALIDATION_ERROR",
                "Unknown dealer.",
                [{"field": "dealer_id", "message": "No dealer with this id exists."}],
            )

        if not getattr(settings, "GEMINI_API_KEY", ""):
            return error_response(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ASSISTANT_NOT_CONFIGURED",
                "The assistant is not configured.",
            )

        # The session is bound to its first dealer. Requests for the same session run one
        # after the other; other sessions are never blocked while this one waits for the LLM.
        context = ToolContext(dealer=dealer)
        try:
            with session_store.open(session_id, dealer_id) as session:
                try:
                    result = run_assistant(
                        message=message,
                        context=context,
                        history=list(session.history),
                    )
                except Exception:
                    logger.exception("Assistant chat failed for session %s", session_id)
                    return error_response(
                        status.HTTP_502_BAD_GATEWAY,
                        "ASSISTANT_UNAVAILABLE",
                        "The assistant could not complete the request. Please try again.",
                    )
                session.commit(trim_history(result["history"], MAX_HISTORY_MESSAGES))
        except SessionDealerMismatch:
            return error_response(
                status.HTTP_400_BAD_REQUEST,
                "VALIDATION_ERROR",
                "This session belongs to a different dealer.",
                [{
                    "field": "session_id",
                    "message": "This session is already bound to a different dealer.",
                }],
            )
        except SessionBusy:
            return error_response(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "ASSISTANT_BUSY",
                "Another request for this session is still running. Please try again.",
            )

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
