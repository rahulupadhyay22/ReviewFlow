# CSV Import

Implements `integrations.csv_import.adapter.CsvImportAdapter`, for bulk
import of past or offline transactions. Each row is normalized the same
way a single webhook event would be, and is subject to the same
`(location_id, external_transaction_id)` idempotency, so re-uploading a
file is safe.

## Connect

`POST /integrations/csv/connect` — session, OWNER/ADMIN. Rejects any
client-supplied `credentials` with `422`; there is no signing secret for
this provider.

## Columns

Required: `external_transaction_id`, `amount`, `currency`, `occurred_at`.
Optional: `customer_phone`, `customer_name`, `payment_method`,
`external_location_id`. A blank cell is treated as missing (the same rule
`SaleCreated` uses for a blank phone).

## Upload

`POST /integrations/{id}/csv-imports` — session, OWNER/ADMIN,
`multipart/form-data`, field `file`.

- **Limits** (environment-configurable): `CSV_IMPORT_MAX_BYTES` (default
  5 MB), `CSV_IMPORT_MAX_ROWS` (default 10,000).
- **Whole-file synchronous validation** before anything is uploaded or
  stored: UTF-8 decoding (a BOM is accepted), the required columns, and
  every row parsed/normalized through `CsvImportAdapter`. Any invalid row
  rejects the whole file with `422` and
  `field_errors: { "row_<n>": [<safe error code>], ... }` for the first 50
  bad rows (1-indexed data rows).
- A valid file is staged in Cloudflare R2 under a server-built key
  (`csv-imports/{merchant_id}/{uuid4}.csv` — never derived from client
  input) and the response is `202 { rows: <count> }`.

## Processing

A background task (`integrations.tasks.import_csv`, queue `events`)
records one `IntegrationEvent` per row, using the same deterministic
idempotency key as the Generic Webhook. The staged object is deleted only
**after every row has been recorded**:

- A task that fails partway through (e.g. a database error) leaves the
  object in place; the rows already recorded stay recorded. Re-running the
  import (the object still exists) completes the remaining rows without
  duplicating the ones already done, then deletes the object.
- A task run against an already-purged object is a no-op.
- An R2 bucket lifecycle rule expiring objects under `csv-imports/` after
  1 day is the operational backstop for a run that never completes.

No row-level failure (e.g. an unresolved location) stops the file: each
row's `IntegrationEvent` goes through the ordinary Phase 04 pipeline
independently and can end `FAILED`/`DEAD_LETTER` on its own without
affecting the others.
