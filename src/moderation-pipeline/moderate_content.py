"""
Runs Rekognition content moderation on the uploaded object. Images are
scanned directly from S3; PDFs are rasterized page-by-page in memory
first, since Rekognition only accepts images, and each page is scanned
in turn -- a single flagged page fails the whole document.

Rasterized pages are scanned via Rekognition's Bytes parameter
directly, without ever being written back to S3, except for the rare
page that rasterizes larger than Rekognition's 5 MB inline-bytes limit,
which gets staged briefly under scratch/ and scanned by S3 reference
instead.

This function does not make the malware determination -- that's
check_malware_scan, running in a parallel branch. This one only
answers "is the visual content itself okay."
"""
import io
import os

import boto3
from pdf2image import convert_from_bytes

MODERATION_CONFIDENCE_THRESHOLD = float(os.environ.get("MODERATION_CONFIDENCE_THRESHOLD", "80"))
MAX_PDF_PAGES_SCANNED = int(os.environ.get("MAX_PDF_PAGES_SCANNED", "20"))
REKOGNITION_BYTES_LIMIT = 5 * 1024 * 1024  # Rekognition's inline-Bytes cap

_s3 = boto3.client("s3")
_rekognition = boto3.client("rekognition")


def _worst_label(labels: list[dict]) -> dict | None:
    if not labels:
        return None
    return max(labels, key=lambda label: label["Confidence"])


def _scan_image_bytes(image_bytes: bytes) -> dict | None:
    result = _rekognition.detect_moderation_labels(Image={"Bytes": image_bytes})
    return _worst_label(result["ModerationLabels"])


def _scan_s3_object(bucket: str, key: str) -> dict | None:
    result = _rekognition.detect_moderation_labels(
        Image={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    return _worst_label(result["ModerationLabels"])


def _image_to_png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _scan_pdf(bucket: str, key: str) -> dict:
    pdf_bytes = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    pages = convert_from_bytes(
        pdf_bytes,
        dpi=150,
        fmt="png",
        last_page=MAX_PDF_PAGES_SCANNED,
    )

    worst_overall = None
    for page_number, page_image in enumerate(pages, start=1):
        page_bytes = _image_to_png_bytes(page_image)

        if len(page_bytes) <= REKOGNITION_BYTES_LIMIT:
            worst = _scan_image_bytes(page_bytes)
        else:
            scratch_key = f"scratch/{key}-page-{page_number}.png"
            _s3.put_object(Bucket=bucket, Key=scratch_key, Body=page_bytes)
            try:
                worst = _scan_s3_object(bucket, scratch_key)
            finally:
                _s3.delete_object(Bucket=bucket, Key=scratch_key)

        if worst and (worst_overall is None or worst["Confidence"] > worst_overall["Confidence"]):
            worst_overall = worst
            worst_overall["page"] = page_number

    return _to_verdict(worst_overall)


def _scan_image(bucket: str, key: str) -> dict:
    return _to_verdict(_scan_s3_object(bucket, key))


def _to_verdict(worst_label: dict | None) -> dict:
    if worst_label is None or worst_label["Confidence"] < MODERATION_CONFIDENCE_THRESHOLD:
        return {"contentStatus": "CLEAN"}

    return {
        "contentStatus": "FLAGGED",
        "category": worst_label["Name"],
        "confidence": worst_label["Confidence"],
        "page": worst_label.get("page"),
    }


def handler(event: dict, context) -> dict:
    bucket = event["bucket"]
    key = event["key"]
    file_type = event["fileType"]

    if file_type == "pdf":
        return _scan_pdf(bucket, key)

    return _scan_image(bucket, key)