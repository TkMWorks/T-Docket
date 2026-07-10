"""
PATCH /documents/{documentId}

Updates a document's user-supplied description. The new text is run
through Comprehend's toxicity model synchronously, in this handler,
before it's ever written -- unlike the file itself, which is
moderated asynchronously by the S3-triggered pipeline. Text
moderation is fast enough (single-digit strings, sub-1KB each) that
there's no need for a queue here.
"""
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

from shared.auth import AuthError, get_user_sub
from shared.responses import response

TABLE_NAME = os.environ["TABLE_NAME"]
MAX_DESCRIPTION_LENGTH = 500
# Comprehend returns a 0-1 confidence per category; anything at or
# above this for any category gets the write rejected. Tune based on
# observed false-positive rate for your content.
TOXICITY_THRESHOLD = float(os.environ.get("TOXICITY_THRESHOLD", "0.7"))

_table = boto3.resource("dynamodb").Table(TABLE_NAME)
_comprehend = boto3.client("comprehend")


def _flagged_category(text: str) -> str | None:
    """Returns the name of the worst-scoring toxicity category if it
    crosses the threshold, otherwise None. Blank text is never flagged."""
    if not text.strip():
        return None

    result = _comprehend.detect_toxic_content(
        TextSegments=[{"Text": text}],
        LanguageCode="en",
    )
    labels = result["ResultList"][0]["Labels"]
    worst = max(labels, key=lambda label: label["Score"])

    return worst["Name"] if worst["Score"] >= TOXICITY_THRESHOLD else None


def handler(event: dict, context) -> dict:
    try:
        user_id = get_user_sub(event)
    except AuthError:
        return response(401, {"message": "Unauthorized"})

    document_id = (event.get("pathParameters") or {}).get("documentId")
    if not document_id:
        return response(400, {"message": "documentId path parameter is required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"message": "Request body must be valid JSON"})

    description = body.get("description")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
        return response(400, {
            "message": f"description must be a string of at most {MAX_DESCRIPTION_LENGTH} characters",
        })

    category = _flagged_category(description)
    if category is not None:
        return response(400, {
            "message": "description was rejected by content moderation",
            "category": category,
        })

    try:
        _table.update_item(
            Key={"userId": user_id, "documentId": document_id},
            # attribute_exists(userId) means this only succeeds against
            # a row that already exists under the caller's own
            # partition -- it can't create a new item for someone
            # else's documentId, and it 404s cleanly if there's no
            # matching row at all.
            ConditionExpression="attribute_exists(userId)",
            UpdateExpression="SET description = :description, updatedAt = :updatedAt",
            ExpressionAttributeValues={
                ":description": description,
                ":updatedAt": int(time.time()),
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return response(404, {"message": "Document not found"})
        raise

    return response(200, {"documentId": document_id, "description": description})