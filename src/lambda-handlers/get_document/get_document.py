"""
GET /documents/{documentId}

Fetches a single document. Doubles as the status-polling endpoint the
UI can call after upload to watch a document move from PENDING to
CLEAN, REJECTED, or INFECTED.
"""
import os

import boto3

from shared.auth import AuthError, get_user_sub
from shared.responses import response

TABLE_NAME = os.environ["TABLE_NAME"]

_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def handler(event: dict, context) -> dict:
    try:
        user_id = get_user_sub(event)
    except AuthError:
        return response(401, {"message": "Unauthorized"})

    document_id = (event.get("pathParameters") or {}).get("documentId")
    if not document_id:
        return response(400, {"message": "documentId path parameter is required"})

    # The key is built entirely from the caller's own sub plus the path
    # parameter. If documentId belongs to a different user, this simply
    # finds nothing under this partition -- it can never return
    # another user's item, regardless of what documentId is passed in.
    result = _table.get_item(Key={"userId": user_id, "documentId": document_id})
    item = result.get("Item")

    if item is None:
        return response(404, {"message": "Document not found"})

    return response(200, {
        "documentId": item["documentId"],
        "status": item["status"],
        "fileType": item.get("fileType"),
        "description": item.get("description", ""),
        "rejectionReason": item.get("rejectionReason"),
        "uploadedAt": item.get("uploadedAt"),
        "updatedAt": item.get("updatedAt"),
    })