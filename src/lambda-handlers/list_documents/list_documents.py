"""
GET /documents

Lists documents belonging to the calling user, paginated. The
KeyConditionExpression pins the query to the caller's own partition
(userId from the verified JWT) -- there is no code path in this
handler that can return another user's rows.
"""
import os

import boto3
from boto3.dynamodb.conditions import Key

from shared.auth import AuthError, get_user_sub
from shared.responses import response

TABLE_NAME = os.environ["TABLE_NAME"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def handler(event: dict, context) -> dict:
    try:
        user_id = get_user_sub(event)
    except AuthError:
        return response(401, {"message": "Unauthorized"})

    query_params = event.get("queryStringParameters") or {}

    try:
        limit = min(int(query_params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
    except ValueError:
        return response(400, {"message": "limit must be an integer"})

    query_kwargs: dict = {
        "KeyConditionExpression": Key("userId").eq(user_id),
        "Limit": limit,
    }

    next_token = query_params.get("nextToken")
    if next_token:
        query_kwargs["ExclusiveStartKey"] = {"userId": user_id, "documentId": next_token}

    result = _table.query(**query_kwargs)

    # Note: documentId is a random UUID, not a timestamp, so results
    # come back in whatever order DynamoDB's SK index gives them --
    # not upload order. If the UI needs a chronological list, add a
    # GSI keyed on (userId, uploadedAt) rather than sorting client-side.
    documents = [
        {
            "documentId": item["documentId"],
            "status": item["status"],
            "fileType": item.get("fileType"),
            "description": item.get("description", ""),
            "uploadedAt": item.get("uploadedAt"),
            "updatedAt": item.get("updatedAt"),
        }
        for item in result.get("Items", [])
    ]

    last_key = result.get("LastEvaluatedKey")

    return response(200, {
        "documents": documents,
        "nextToken": last_key["documentId"] if last_key else None,
    })