"""
Triggered directly by an S3 bucket notification (ObjectCreated) on the
staging bucket. Parses the object key -- built by the presign_upload
API handler as staging/{userId}/{documentId}-{filename} -- into
structured input and starts a Step Functions execution to moderate it.

This function does no moderation itself; it only translates a raw S3
event into clean input for the state machine.
"""
import json
import os
import re
import urllib.parse

import boto3
from botocore.exceptions import ClientError

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

_sfn = boto3.client("stepfunctions")

# staging/{userId}/{documentId}-{restOfFilename}
_KEY_PATTERN = re.compile(
    r"^staging/(?P<user_id>[^/]+)/(?P<document_id>[0-9a-fA-F-]{36})-(?P<filename>.+)$"
)

_EXTENSION_TO_FILE_TYPE = {
    "pdf": "pdf",
    "jpg": "jpg",
    "jpeg": "jpg",
    "png": "png",
    "doc": "doc",
    "docx" : "doc",
    "xls" : "xls",
    "xlsx" : "xls",
    "ppt" : "ppt",
    "pptx" : "ppt"
}


def handler(event: dict, context) -> dict:
    started = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 event keys are URL-encoded and use "+" for spaces.
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        match = _KEY_PATTERN.match(key)
        if match is None:
            # Anything not matching our own naming convention doesn't
            # belong to this app -- log and skip rather than fail the
            # whole batch over one unexpected object.
            print(f"Skipping key that doesn't match expected pattern: {key}")
            continue

        user_id = match.group("user_id")
        document_id = match.group("document_id")
        filename = match.group("filename")
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        file_type = _EXTENSION_TO_FILE_TYPE.get(extension)

        if file_type is None:
            print(f"Skipping unrecognized file extension for key: {key}")
            continue

        execution_input = {
            "bucket": bucket,
            "key": key,
            "userId": user_id,
            "documentId": document_id,
            "fileType": file_type,
        }

        try:
            response = _sfn.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                # One execution per document. If S3 redelivers the
                # same event (it can), this reuses the same run
                # instead of moderating the file twice.
                name=f"moderate-{document_id}",
                input=json.dumps(execution_input),
            )
            started.append(response["executionArn"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
                print(f"Execution already exists for {document_id}, skipping")
                continue
            raise

    return {"startedExecutions": started}