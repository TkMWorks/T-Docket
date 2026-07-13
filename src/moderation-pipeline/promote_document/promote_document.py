"""
Runs when evaluate_moderation returns CLEAN: copies the object from
the staging bucket to the final bucket (rewriting the "staging/"
prefix to "documents/"), removes the staging copy, and marks the
DynamoDB record CLEAN.
"""
import os
import time

import boto3

FINAL_BUCKET = os.environ["FINAL_BUCKET"]
TABLE_NAME = os.environ["TABLE_NAME"]

_s3 = boto3.client("s3")
_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def handler(event: dict, context) -> dict:
    bucket = event["bucket"]
    key = event["key"]
    user_id = event["userId"]
    document_id = event["documentId"]

    if not key.startswith("staging/"):
        raise ValueError(f"Expected a staging/ key, got: {key}")

    final_key = "documents/" + key[len("staging/"):]

    _s3.copy_object(
        CopySource={"Bucket": bucket, "Key": key},
        Bucket=FINAL_BUCKET,
        Key=final_key,
    )
    _s3.delete_object(Bucket=bucket, Key=key)

    _table.update_item(
        Key={"userId": user_id, "documentId": document_id},
        UpdateExpression=(
            "SET #status = :status, s3Key = :s3Key, updatedAt = :updatedAt "
            "REMOVE rejectionReason"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "CLEAN",
            ":s3Key": final_key,
            ":updatedAt": int(time.time()),
        },
    )

    return {"status": "CLEAN", "finalKey": final_key}