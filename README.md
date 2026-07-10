# T-Docket
Serverless per-user document storage on AWS with automated malware and content moderation.

## Document API Lambda handlers

Four functions, one per route, meant to sit behind API Gateway HTTP API
with a Cognito JWT authorizer on every route.

| Route | Function | Handler |
|---|---|---|
| `POST /uploads` | `presign_upload` | `presign_upload/handler.handler` |
| `GET /documents` | `list_documents` | `list_documents/handler.handler` |
| `GET /documents/{documentId}` | `get_document` | `get_document/handler.handler` |
| `PATCH /documents/{documentId}` | `update_annotation` | `update_annotation/handler.handler` |

### Runtime configuration

- **Runtime**: `python3.14`
- **Architecture**: `arm64`
- All four handlers are pure Python with no compiled dependencies (only
  `boto3`, which ships with the managed runtime), so arm64 works with
  zero code changes -- there's nothing here that needed an ARM-specific
  build the way `numpy`/`Pillow`/`psycopg2` would.

### Packaging `shared/`

`shared/` (auth + response helpers) is imported by all four handlers.
Rather than duplicating it into four separate zip files, package it as
a **Lambda layer** and attach the layer to all four functions, so each
function's own deployment package is just its `handler.py`:

```
lambda-layer/
└── python/
    └── shared/
        ├── __init__.py
        ├── auth.py
        └── responses.py
```

(The `python/` prefix is required by Lambda's layer convention so the
package lands on `sys.path`.)

### Environment variables

| Function | Variables |
|---|---|
| `presign_upload` | `STAGING_BUCKET`, `TABLE_NAME`, `PRESIGN_EXPIRY_SECONDS` (optional, default `300`) |
| `list_documents` | `TABLE_NAME` |
| `get_document` | `TABLE_NAME` |
| `update_annotation` | `TABLE_NAME`, `TOXICITY_THRESHOLD` (optional, default `0.7`) |

### IAM policy per function (least privilege)

- **`presign_upload`**: `dynamodb:PutItem` on the table; `s3:PutObject`
  on `staging/*` in the staging bucket only. Does not need
  `s3:GetObject` or any access to the final bucket.
- **`list_documents`**: `dynamodb:Query` on the table only.
- **`get_document`**: `dynamodb:GetItem` on the table only.
- **`update_annotation`**: `dynamodb:UpdateItem` on the table;
  `comprehend:DetectToxicContent` (this action can't be scoped to a
  resource ARN -- Comprehend only supports `"Resource": "*"` for it).

None of the four need broader table or bucket access than this --
in particular, none of them need `s3:GetObject`/`s3:PutObject` on the
**final** `documents/*` prefix. If you add a presigned-GET endpoint for
downloads later, that's a fifth, separate function with its own
narrow `s3:GetObject` grant on `documents/*` only.

### Important: Comprehend toxicity detection region availability

`comprehend:DetectToxicContent` was launched in a limited set of
regions (US East N. Virginia, US West Oregon, Europe Ireland, Asia
Pacific Sydney at initial release). If your stack runs somewhere else
(for example `ap-south-1` for lower latency to Bhubaneswar), check the
current [Comprehend endpoints and quotas
page](https://docs.aws.amazon.com/general/latest/gr/comprehend.html)
before wiring this up -- you may need to invoke it cross-region via a
regional `boto3` client, which adds latency to every `PATCH` call, or
drop the synchronous check and route descriptions through the async
pipeline instead.

### Client contract

**`POST /uploads`**
```json
// Request
{ "filename": "tax-return.pdf", "contentType": "application/pdf", "description": "Tax returns for FY2025-26" }
// Response (201)
{ "documentId": "...", "uploadUrl": "https://...", "s3Key": "staging/{sub}/...", "expiresIn": 300 }
```
The client must PUT the file to `uploadUrl` with a `Content-Type`
header matching what was sent to `/uploads` -- the presigned URL is
signed against that content type and S3 will reject a mismatch.

**`GET /documents?limit=50&nextToken=...`** → `{ "documents": [...], "nextToken": "..." }`

**`GET /documents/{documentId}`** → single document, `status` field
drives UI polling (`PENDING` → `CLEAN` / `REJECTED` / `INFECTED`)

**`PATCH /documents/{documentId}`**
```json
// Request
{ "description": "Corrected description" }
// Response (200) or (400) if Comprehend flags it: { "message": "...", "category": "HATE_SPEECH" }
```