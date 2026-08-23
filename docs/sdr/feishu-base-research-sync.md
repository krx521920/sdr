# Feishu Base research-result sync

Completed SDR research can be synchronized to one tenant-owned Feishu Base
table through the official Bitable v1 OpenAPI. This is separate from the
existing Feishu custom-bot sales alert: the bot posts a notification, while the
Base integration maintains a structured research record.

## Setup

1. Create a Feishu custom app and grant the Base table, field, and record
   read/write permissions needed by the integration.
2. Add the app to the target Base. If advanced permissions are enabled, give
   the app management permission for that Base.
3. In **Settings > Automation Operations > Feishu Base research sync**, enter:
   - app ID and app secret;
   - the Base `app_token` and target `table_id`;
   - explicit mappings from SDR values to existing target field names.
4. Map `intake_id` to a unique text field. This is the stable business key used
   to search before every create or update.
5. Save, run **Validate credentials & fields**, then enable automatic sync.

The app secret is encrypted at rest and is never returned by the API. The
OpenAPI host is deployment-owned through `FEISHU_OPEN_API_BASE_URL`; tenant
configuration cannot redirect server-side requests.

## Supported field mappings

`intake_id` is required. All other keys are optional:

- identity: `company_name`, `contact_name`, `email`, `phone`, `linkedin_url`,
  `website`;
- source: `source`, `source_record_id`;
- research: `research_summary`, `research_facts`, `source_urls`,
  `inspection_status`;
- qualification: `qualification_score`, `qualification_band`,
  `qualification_reasons`;
- handoff: `assigned_sales`, `routing_reason`, `crm_lead_id`, `processed_at`.

Research facts, source URLs, and qualification reasons are serialized as JSON
text. Score requires a number field and processed time requires a date/time
field. URL and phone values can target their native field types or text fields.
Formula, lookup, attachment, and system fields are rejected by validation.

## Delivery guarantees

The synchronization uses the shared `automation_job` ledger under the job name
`feishu_base.sync_research_result`:

- the record search and write are scoped to the configured Base and table;
- the intake UUID is used as a business key, so retries update rather than
  duplicate records;
- a payload hash makes unchanged scheduling idempotent;
- changed research or mapping creates a new durable job and updates the same
  Base row;
- rate limits, write conflicts, temporary data-not-ready responses, timeouts,
  and server failures are retried with the shared exponential backoff;
- invalid credentials, missing fields, type mismatches, and duplicate business
  keys become actionable dead-letter jobs;
- the minute-level response reconciliation pass recovers completed intakes that
  were missed during broker or worker interruption.

Operational state is stored in `integration_feishu_base_sync`. It contains the
provider record ID, hashes, attempt count, synchronized field names, timestamps,
and sanitized error details, but not access tokens or the app secret.

Official API references:

- [Tenant access token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
- [Search Base records](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/bitable-v1/app-table-record/search)
- [Create a Base record](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create)
