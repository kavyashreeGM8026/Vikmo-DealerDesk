"""One error shape for the whole API (contract section 3).

Every failure the API returns looks like:

    {"error": {"code": "VALIDATION_ERROR", "message": "...", "details": [...]}}

DRF calls `api_exception_handler` whenever a view raises, so views never have to build
error responses by hand: they just `raise` and this module turns it into the shape above.
"""
import logging

from django.http import Http404
from rest_framework.exceptions import APIException, NotFound, ParseError, ValidationError
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Base class for business-rule errors that map onto the contract's error codes."""

    status_code = 400
    code = "VALIDATION_ERROR"
    default_message = "Bad request."

    def __init__(self, message=None, details=None):
        self.message = message or self.default_message
        self.details = details if details is not None else []
        super().__init__(self.message)


class NotFoundError(APIError):
    status_code, code = 404, "NOT_FOUND"
    default_message = "Resource not found."


class DuplicateError(APIError):
    status_code, code = 409, "DUPLICATE"
    default_message = "Resource already exists."


def error_response(status_code, code, message, details=None):
    return Response(
        {"error": {"code": code, "message": message, "details": details or []}},
        status=status_code,
    )


def flatten_errors(detail, field=None):
    """Turn DRF's nested validation errors into a flat list of
    {"field": "items[0].quantity", "message": "..."} entries."""
    out = []
    if isinstance(detail, dict):
        for key, value in detail.items():
            name = f"{field}.{key}" if field else str(key)
            out.extend(flatten_errors(value, name))
    elif isinstance(detail, (list, tuple)):
        for index, item in enumerate(detail):
            if isinstance(item, (dict, list, tuple)):
                name = f"{field}[{index}]" if field else f"[{index}]"
                out.extend(flatten_errors(item, name))
            else:
                out.append({"field": field, "message": str(item)})
    else:
        out.append({"field": field, "message": str(detail)})
    return out


_STATUS_TO_CODE = {
    400: "VALIDATION_ERROR",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    415: "UNSUPPORTED_MEDIA_TYPE",
}


def api_exception_handler(exc, context):
    # 1. Our own business errors (duplicate, not found, ...)
    if isinstance(exc, APIError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    # 2. "Does not exist": get_object_or_404, or an out-of-range page number
    if isinstance(exc, (Http404, NotFound)):
        message = str(exc.detail) if isinstance(exc, NotFound) else "Resource not found."
        return error_response(404, "NOT_FOUND", message)

    # 3. Bad input: serializer validation failures and malformed JSON
    if isinstance(exc, (ValidationError, ParseError)):
        details = flatten_errors(exc.detail)
        message = "Invalid request data."
        if details:
            where = f"{details[0]['field']}: " if details[0]["field"] else ""
            message = f"Invalid request data. {where}{details[0]['message']}"
        return error_response(400, "VALIDATION_ERROR", message, details)

    # 4. Other DRF errors (405 wrong method, 415 wrong content type, ...)
    if isinstance(exc, APIException):
        code = _STATUS_TO_CODE.get(exc.status_code, "ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    # 5. A genuine bug: log the traceback for us, tell the client nothing sensitive
    logger.exception("Unhandled error in API view", exc_info=exc)
    return error_response(500, "INTERNAL_ERROR", "Internal server error.")
