
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import error_response
from core.services.supplier_sync import (
    SupplierSyncError,
    sync_supplier_catalogue,
)


class SupplierSyncView(APIView):
    """POST /api/sync/supplier/ — synchronize the supplier catalogue."""

    def post(self, request):
        try:
            result = sync_supplier_catalogue()
        except SupplierSyncError as exc:
            return error_response(
                502,
                "SUPPLIER_UNAVAILABLE",
                str(exc),
            )

        return Response(result, status=200)
