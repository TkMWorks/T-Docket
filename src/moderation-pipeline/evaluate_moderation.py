"""
Combines the malware-scan branch and content-moderation branch results
-- collected by the Step Functions Parallel state into
$.moderationResults -- into a single verdict that promote_document or
reject_document acts on.
"""


def handler(event: dict, context) -> dict:
    malware_result, content_result = event["moderationResults"]

    malware_status = malware_result.get("malwareScanStatus", "INCONCLUSIVE")

    if malware_status == "THREATS_FOUND":
        return {"status": "INFECTED", "reason": "malware detected"}

    if malware_status == "INCONCLUSIVE":
        # The malware-scan branch hit its retry limit without
        # GuardDuty ever tagging the object. Fail safe: don't promote
        # a document GuardDuty never confirmed as clean.
        return {"status": "REJECTED", "reason": "malware scan inconclusive"}

    if content_result.get("contentStatus") == "FLAGGED":
        return {
            "status": "REJECTED",
            "reason": content_result.get("category", "objectionable content"),
        }

    return {"status": "CLEAN", "reason": None}