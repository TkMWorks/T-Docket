"""
Runs when evaluate_moderation returns REJECTED or INFECTED (or when
the Parallel state itself fails unexpectedly -- see the Catch in
state_machine.asl.json): deletes the staging object and records the
reason on the DynamoDB row so the UI can show the user why.
"""
import os
import time

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]

_s3 = boto3.client("s3")
_table = boto3.resource("dynamodb").Table(TABLE_NAME)

_DEFAULT_VERDICT = {"status": "REJECTED", "reason": "moderation pipeline error"}


def handler(event: dict, context) -> dict:
    bucket = event["bucket"]
    key = event["key"]
    user_id = event["userId"]
    document_id = event["documentId"]
    # Defensive default: if this runs because the Parallel state
    # itself failed (Rekognition outage, corrupted PDF, etc.) rather
    # than because evaluate_moderation produced a verdict, there is no
    # $.verdict on the event at all.
    verdict = event.get("verdict", _DEFAULT_VERDICT)

    _s3.delete_object(Bucket=bucket, Key=key)

    _table.update_item(
        Key={"userId": user_id, "documentId": document_id},
        UpdateExpression="SET #status = :status, rejectionReason = :reason, updatedAt = :updatedAt",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": verdict["status"],
            ":reason": verdict.get("reason"),
            ":updatedAt": int(time.time()),
        },
    )

    return {"status": verdict["status"], "reason": verdict.get("reason")}