"""Shared helpers for building API Gateway HTTP API responses."""
import json
from decimal import Decimal
from typing import Any


class _DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns numbers as Decimal; json.dumps chokes on that
    by default, so this coerces to int/float for the response body."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def response(status_code: int, body: Any = None, headers: dict | None = None) -> dict:
    """Builds a Lambda proxy integration response for API Gateway
    HTTP API (payload format 2.0)."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            **(headers or {}),
        },
        "body": json.dumps(body if body is not None else {}, cls=_DecimalEncoder),
    }