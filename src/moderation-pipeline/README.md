# Moderation pipeline

Six Lambda functions orchestrated by `state_machine.asl.json`, triggered
by an S3 `ObjectCreated` notification on the staging bucket pointed
directly at `s3_event_router` (no EventBridge needed for the trigger).

## Change from the original design sketch

Earlier planning named four modules: `s3_event_router`, `pdf_rasterizer`,
`promote_document`, `reject_document`. Two more turned out to be
necessary once the actual data flow got worked out:

- **`pdf_rasterizer` was folded into `moderate_content`.** Rekognition
  accepts image bytes directly, so rasterizing a PDF page and scanning
  it happen in the same invocation — no scratch objects written to S3
  for the common case (only the rare >5 MB page falls back to a
  temporary S3 object, immediately deleted after the scan).
- **`evaluate_moderation` is new.** Reconciling "GuardDuty says X" and
  "Rekognition says Y" into one `CLEAN` / `REJECTED` / `INFECTED`
  verdict is its own small piece of logic, kept separate from the
  branches that produce each half.
- **`check_malware_scan` is new.** GuardDuty doesn't finish scanning by
  the time the S3 upload completes, so this function exists purely to
  poll the object's tags — it's invoked repeatedly by a Step Functions
  `Retry` policy on a custom exception, not by application-level retry
  logic.

## Function map

| Function | Trigger | Purpose |
|---|---|---|
| `s3_event_router` | S3 `ObjectCreated` on staging bucket | Parses the key, starts the state machine execution |
| `check_malware_scan` | Step Functions Task (retried) | Polls for the GuardDuty scan tag |
| `moderate_content` | Step Functions Task | Rekognition on images / rasterized PDF pages |
| `evaluate_moderation` | Step Functions Task | Combines both branch results into a verdict |
| `promote_document` | Step Functions Task | Copies to final bucket, marks `CLEAN` |
| `reject_document` | Step Functions Task | Deletes staging object, records the reason |

## Environment variables

| Function | Variables |
|---|---|
| `s3_event_router` | `STATE_MACHINE_ARN` |
| `check_malware_scan` | — |
| `moderate_content` | `MODERATION_CONFIDENCE_THRESHOLD` (default `80`), `MAX_PDF_PAGES_SCANNED` (default `20`) |
| `evaluate_moderation` | — |
| `promote_document` | `FINAL_BUCKET`, `TABLE_NAME` |
| `reject_document` | `TABLE_NAME` |

## IAM policy per function (least privilege)

- **`s3_event_router`**: `states:StartExecution` on the state machine only. Also needs a resource-based (Lambda) policy granting `s3.amazonaws.com` invoke permission, scoped via `SourceArn` to the staging bucket.
- **`check_malware_scan`**: `s3:GetObjectTagging` on the staging bucket.
- **`moderate_content`**: `s3:GetObject` on the staging bucket; `s3:PutObject`/`s3:DeleteObject` scoped to `scratch/*` only (the oversized-page fallback); `rekognition:DetectModerationLabels` — this action has no resource-level ARN support, so it's necessarily `"Resource": "*"`.
- **`evaluate_moderation`**: no AWS resource permissions beyond basic execution (CloudWatch Logs) — it's pure logic over its input.
- **`promote_document`**: `s3:GetObject`/`s3:DeleteObject` on the staging bucket, `s3:PutObject` on the final bucket, `dynamodb:UpdateItem` on the table.
- **`reject_document`**: `s3:DeleteObject` on the staging bucket, `dynamodb:UpdateItem` on the table.
- **State machine execution role**: `lambda:InvokeFunction` on all six function ARNs.

## Building the poppler layer for `moderate_content`

`pdf2image` shells out to the `pdftoppm` binary — it isn't a Python
package, so it can't be `pip install`ed. Build it as a separate Lambda
layer targeting arm64, using the official Lambda base image so the
binary is linked against the same libraries the runtime provides:

```dockerfile
FROM public.ecr.aws/lambda/python:3.14-arm64
RUN dnf install -y poppler-utils && \
    mkdir -p /opt/bin /opt/lib && \
    cp /usr/bin/pdftoppm /usr/bin/pdftocairo /opt/bin/ && \
    cp -r /usr/lib64/libpoppler* /opt/lib/ 2>/dev/null || true
```

Run a container from this image, copy `/opt` out to a local
`layer/` directory, zip it, and publish as a layer. Lambda
automatically adds `/opt/bin` to `PATH` and `/opt/lib` to
`LD_LIBRARY_PATH` for every function the layer is attached to, so
`pdf2image` finds `pdftoppm` with no extra configuration.

`pdf2image` and `Pillow` themselves (see `requirements.txt` in
`moderate_content/`) are ordinary Python packages — install them into
the function's own deployment package (or a second, pure-Python
layer), no Docker needed for those two. Double check both have
published `cp314`/`manylinux2014_aarch64` wheels at deploy time —
Python 3.14 is recent enough that some packages may still lag on
prebuilt wheels for it.

## Safety nets already assumed elsewhere

- An S3 Lifecycle rule on the staging bucket (covered earlier in this
  project) auto-expires objects after 24–48 hours, bounding how long a
  document can sit `PENDING` even in an unforeseen failure mode this
  pipeline's own `Catch` blocks don't cover.