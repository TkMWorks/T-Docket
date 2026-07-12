# Document Storage API — Documentation

## 1. Overview

A document storage service where each authenticated user uploads PDFs
and images into their own isolated space, annotates them with a short
description, and has every upload screened for malware and
objectionable content before it becomes visible. This document covers
the public-facing API (what the UI calls) and the project layout
behind it.

## 2. Project structure

```
document-storage-app/
├── lambda_handlers/                    # API-layer Lambda source — BUILT
│   ├── shared/                         # Packaged as a Lambda layer,
│   │   │                               # attached to all four functions below
│   │   ├── __init__.py
│   │   ├── auth.py                     # Extracts verified sub from JWT claims
│   │   └── responses.py                # Consistent API Gateway response shape
│   ├── presign_upload/
│   │   └── presign_upload.py           # POST /uploads
│   ├── list_documents/
│   │   └── list_documents.py           # GET /documents
│   ├── get_document/
│   │   └── get_document.py             # GET /documents/{documentId}
│   ├── update_annotation/
│   │   └── update_annotation.py        # PATCH /documents/{documentId}
│   └── README.md                       # Runtime, packaging, IAM notes
│
├── moderation_pipeline/                # Async pipeline — BUILT
│   ├── s3_event_router/
│   │   └── s3_event_router.py          # Starts a Step Functions execution per upload
│   ├── check_malware_scan/
│   │   └── check_malware_scan.py       # Polls the GuardDuty scan tag (retried)
│   ├── moderate_content/
│   │   ├── moderate_content.py         # Rekognition on images / rasterized PDF pages
│   │   └── requirements.txt
│   ├── evaluate_moderation/
│   │   └── evaluate_moderation.py      # Combines both branches into one verdict
│   ├── promote_document/
│   │   └── promote_document.py         # Copies clean files to the final bucket
│   ├── reject_document/
│   │   └── reject_document.py          # Deletes staging object, records reason
│   ├── state_machine.asl.json          # Step Functions definition wiring it together
│   └── README.md                       # IAM, env vars, poppler layer build notes
│
├── infra/                              # Terraform — NOT YET BUILT
│   ├── cognito.tf
│   ├── s3.tf
│   ├── dynamodb.tf
│   ├── api_gateway.tf
│   ├── step_functions.tf
│   └── iam.tf
│
└── openapi.yaml                        # API contract — BUILT (this delivery)
```

## 3. AWS resource inventory

| Resource | Role | Notes |
|---|---|---|
| Cognito User Pool | Authentication | Issues the JWT every API call carries |
| API Gateway (HTTP API) | Routing | Cognito JWT authorizer on every route |
| `presign_upload` Lambda | Compute | Backs `POST /uploads` |
| `list_documents` Lambda | Compute | Backs `GET /documents` |
| `get_document` Lambda | Compute | Backs `GET /documents/{documentId}` |
| `update_annotation` Lambda | Compute | Backs `PATCH /documents/{documentId}` |
| S3 staging bucket | Storage | Unmoderated uploads, `staging/{sub}/*` |
| S3 final bucket | Storage | Moderated, "live" documents, `documents/{sub}/*` |
| DynamoDB `Documents` table | Storage | `PK userId`, `SK documentId` — metadata, status, annotation |
| Step Functions state machine | Orchestration | Runs the async moderation workflow |
| GuardDuty Malware Protection for S3 | Security | Scans staging objects for malware |
| Rekognition | Security | Content moderation on images / rasterized PDF pages |
| Comprehend | Security | Synchronous toxicity check on annotation text |

## 4. Authentication

Every endpoint requires a valid Cognito-issued JWT in the `Authorization` header:

```
Authorization: Bearer <id_token>
```

API Gateway's Cognito JWT authorizer verifies the token's signature and
expiry *before* any Lambda runs. Handler code only ever reads the
already-verified `sub` claim — it performs no verification of its own,
and never trusts a `userId` supplied in a request body or path.

A request with a missing, expired, or invalid token never reaches a
handler; API Gateway returns `401` directly.

## 5. Common response shape

All responses are `Content-Type: application/json`.

**Errors** are always:
```json
{ "message": "human-readable description" }
```
The `update_annotation` endpoint's moderation rejection adds one field:
```json
{ "message": "description was rejected by content moderation", "category": "HATE_SPEECH" }
```

## 6. Endpoints

### `POST /uploads`

Creates a `PENDING` document record and returns a presigned URL scoped
to one S3 object. The client uploads the file directly to S3 using
that URL; moderation happens afterward, asynchronously.

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `filename` | string | yes | Used to build the S3 key; path components are stripped |
| `contentType` | string | yes | One of `application/pdf`, `image/jpeg`, `image/png` |
| `description` | string | no | Max 500 characters |

```json
{
  "filename": "tax-return.pdf",
  "contentType": "application/pdf",
  "description": "Tax returns for FY2025-26"
}
```

**Success — `201 Created`**

```json
{
  "documentId": "3f9c2e1a-...",
  "uploadUrl": "https://staging-bucket.s3.amazonaws.com/staging/{sub}/3f9c2e1a-...-tax-return.pdf?X-Amz-...",
  "s3Key": "staging/{sub}/3f9c2e1a-...-tax-return.pdf",
  "expiresIn": 300
}
```

The client must then `PUT` the file to `uploadUrl` with a `Content-Type`
header **exactly matching** what was sent above — the URL is signed
against that content type and S3 rejects a mismatch.

**Errors**

| Status | Condition |
|---|---|
| `400` | `filename` missing, `contentType` not in the allowlist, or `description` over 500 characters |
| `401` | Missing or invalid JWT |
| `409` | Document ID collision (astronomically rare UUID clash) — retry |

**Example**

```bash
curl -X POST https://api.example.com/uploads \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"tax-return.pdf","contentType":"application/pdf","description":"Tax returns for FY2025-26"}'
```

---

### `GET /documents`

Lists documents belonging to the calling user, paginated.

**Query parameters**

| Param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `limit` | integer | no | `50` | Capped at `100` |
| `nextToken` | string | no | — | `documentId` of the last item from a previous page |

**Success — `200 OK`**

```json
{
  "documents": [
    {
      "documentId": "3f9c2e1a-...",
      "status": "CLEAN",
      "fileType": "pdf",
      "description": "Tax returns for FY2025-26",
      "uploadedAt": 1751980800,
      "updatedAt": 1751980920
    }
  ],
  "nextToken": null
}
```

Results are **not** guaranteed to be in upload order — `documentId` is
a random UUID, not a timestamp. A chronological listing would need a
GSI on `(userId, uploadedAt)`; see the note in `list_documents/list_documents.py`.

**Errors**

| Status | Condition |
|---|---|
| `400` | `limit` is not a valid integer |
| `401` | Missing or invalid JWT |

---

### `GET /documents/{documentId}`

Fetches a single document. This is also the status-polling endpoint —
the UI can call it after upload to watch `status` move from `PENDING`
to `CLEAN`, `REJECTED`, or `INFECTED`.

**Path parameters**

| Param | Type | Required |
|---|---|---|
| `documentId` | string | yes |

**Success — `200 OK`**

```json
{
  "documentId": "3f9c2e1a-...",
  "status": "REJECTED",
  "fileType": "pdf",
  "description": "Tax returns for FY2025-26",
  "rejectionReason": "GRAPHIC",
  "uploadedAt": 1751980800,
  "updatedAt": 1751981100
}
```

`rejectionReason` is only present once moderation has flagged the
document; it's `null` for `PENDING` or `CLEAN` documents.

**Errors**

| Status | Condition |
|---|---|
| `400` | `documentId` path parameter missing |
| `401` | Missing or invalid JWT |
| `404` | No document with that ID under the caller's own account — this is also the response if the ID belongs to someone else, by design |

---

### `PATCH /documents/{documentId}`

Updates a document's description. Runs the new text through
Comprehend's toxicity model **synchronously** before writing it —
independent of the async pipeline that moderates the file itself.

**Path parameters**

| Param | Type | Required |
|---|---|---|
| `documentId` | string | yes |

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `description` | string | yes | Max 500 characters |

```json
{ "description": "Tax returns for FY2025-26, amended" }
```

**Success — `200 OK`**

```json
{ "documentId": "3f9c2e1a-...", "description": "Tax returns for FY2025-26, amended" }
```

**Errors**

| Status | Condition |
|---|---|
| `400` | `description` missing, not a string, or over 500 characters |
| `400` | Description flagged by Comprehend — response includes `category` (one of `GRAPHIC`, `HARASSMENT_OR_ABUSE`, `HATE_SPEECH`, `INSULT`, `PROFANITY`, `SEXUAL`, `VIOLENCE_OR_THREAT`) |
| `401` | Missing or invalid JWT |
| `404` | No document with that ID under the caller's own account |

## 7. Status code reference

| Code | Meaning | Used by |
|---|---|---|
| `200` | Success | `GET /documents`, `GET /documents/{id}`, `PATCH /documents/{id}` |
| `201` | Resource created | `POST /uploads` |
| `400` | Validation failure or moderation rejection | All endpoints |
| `401` | Missing/invalid JWT | All endpoints (enforced by API Gateway) |
| `404` | Document not found or not owned by caller | `GET /documents/{id}`, `PATCH /documents/{id}` |
| `409` | Document ID collision | `POST /uploads` |

## 8. Internal moderation pipeline (not part of the public API)

After `POST /uploads` completes, an S3 event on the staging bucket
triggers a Step Functions workflow — GuardDuty malware scan and
Rekognition content moderation run in parallel, and the result is
written back to the same DynamoDB row the public API reads. The UI
never calls this pipeline directly; it only ever sees the outcome
through `status` on `GET /documents/{documentId}`. Full design covered
earlier in this conversation — code for this half isn't built yet.