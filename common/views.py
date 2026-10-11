"""Django-level 404/500 pages as JSON, so even unknown URLs use the contract's error shape."""
from django.http import JsonResponse


def _json_error(status, code, message):
    return JsonResponse({"error": {"code": code, "message": message, "details": []}}, status=status)


def not_found(request, exception=None):
    return _json_error(404, "NOT_FOUND", "Resource not found.")


def server_error(request):
    return _json_error(500, "INTERNAL_ERROR", "Internal server error.")
