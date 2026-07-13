"""
POST /uploads

Creates a PENDING document record and returns a presigned URL scoped
to a single S3 key, which the client uses to PUT the file directly
into the staging bucket. Moderation happens later, asynchronously,
off an S3 event on that bucket -- this handler's only job is to hand
out a narrowly-scoped upload URL and record the document's metadata.
"""
import json
import os
import time
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from shared.auth import AuthError, get_user_sub
from shared.responses import response

STAGING_BUCKET = os.environ["STAGING_BUCKET"]
TABLE_NAME = os.environ["TABLE_NAME"]
PRESIGN_EXPIRY_SECONDS = int(os.environ.get("PRESIGN_EXPIRY_SECONDS", "300"))
MAX_DESCRIPTION_LENGTH = 500

# contentType -> file extension used in the S3 key. Anything not in
# this map is rejected outright; this is the same allowlist the
# moderation pipeline expects to see arrive in staging.
ALLOWED_CONTENT_TYPES = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
}

# Explicit SigV4 signing, regional endpoint. Module-level so the
# clients are reused across warm invocations instead of rebuilt
# every request.
_s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def handler(event: dict, context) -> dict:
    try:
        user_id = get_user_sub(event)
    except AuthError:
        return response(401, {"message": "Unauthorized"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"message": "Request body must be valid JSON"})

    filename = body.get("filename")
    content_type = body.get("contentType")
    description = body.get("description", "")

    if not filename or not isinstance(filename, str):
        return response(400, {"message": "filename is required"})

    if content_type not in ALLOWED_CONTENT_TYPES:
        return response(400, {
            "message": f"contentType must be one of {sorted(ALLOWED_CONTENT_TYPES)}",
        })

    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
        return response(400, {
            "message": f"description must be a string of at most {MAX_DESCRIPTION_LENGTH} characters",
        })

    document_id = str(uuid.uuid4())
    extension = ALLOWED_CONTENT_TYPES[content_type]
    safe_filename = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    s3_key = f"staging/{user_id}/{document_id}-{safe_filename}"
    now = int(time.time())

    try:
        _table.put_item(
            Item={
                "userId": user_id,
                "documentId": document_id,
                "s3Key": s3_key,
                "status": "PENDING",
                "fileType": extension,
                "description": description,
                "uploadedAt": now,
                "updatedAt": now,
            },
            # documentId is a fresh UUID, so this only ever trips on
            # an astronomically unlikely collision -- but it's a one-
            # line guard against silently overwriting a record.
            ConditionExpression="attribute_not_exists(documentId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return response(409, {"message": "Document ID collision, please retry"})
        raise

    upload_url = _s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": STAGING_BUCKET,
            "Key": s3_key,
            "ContentType": content_type,
        },
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )

    return response(201, {
        "documentId": document_id,
        "uploadUrl": upload_url,
        "s3Key": s3_key,
        "expiresIn": PRESIGN_EXPIRY_SECONDS,
    })