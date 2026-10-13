from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import NotFoundError
from core.services import reorder


class ForecastView(APIView):
    """GET /api/forecast/{sku}/ (contract section 10.2). Read-only."""

    http_method_names = ["get", "head", "options"]

    def get(self, request, sku):
        rows = reorder.get_forecast(sku)
        if rows is None:
            raise NotFoundError(f"No forecast for SKU '{sku}'.")
        return Response({"sku": sku, "forecast": rows})


class ReorderSuggestionsView(APIView):
    """GET /api/reorder-suggestions/ - one entry per forecast SKU, not paginated, live stock."""

    http_method_names = ["get", "head", "options"]

    def get(self, request):
        return Response({"results": reorder.reorder_suggestions()})
