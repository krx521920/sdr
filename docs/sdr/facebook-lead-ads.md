# Facebook Lead Ads intake

The Facebook provider receives signed Page `leadgen` webhooks, acknowledges
them quickly, fetches the complete lead from the version-pinned Meta Graph API,
and sends the normalized candidate through the same tenant-scoped SDR pipeline
used by website forms.

## Delivered scope

- Organization administrators can authorize Meta from the CRM settings page,
  review the Pages they manage, and choose which Pages belong to the company
  workspace. A manual Page-token endpoint remains available as an operations
  fallback.
- Page tokens are encrypted at rest and never returned by the API.
- The public webhook supports Meta's verification challenge and validates
  `X-Hub-Signature-256` against the raw request body.
- Lead events are queued in Celery, retried with exponential backoff for Graph
  rate limits and transient failures, and deduplicated by Meta `leadgen_id`.
- Standard and custom form questions are preserved. Standard identity and
  company fields are mapped into the shared SDR domain before scoring and CRM
  handoff.
- Optional Conversion Leads feedback returns raw, qualified, and converted CRM
  funnel stages to Meta using the original Lead Ads `leadgen_id`. The payload
  deliberately excludes email, phone, and other customer PII.
- Page Messenger intake can be enabled independently for each connected Page.
  Signed private-message webhooks are persisted before durable processing. The
  first message creates a lead through the shared SDR pipeline; later messages
  update the same Page-scoped conversation and CRM lead.
- OAuth state is signed, expires after 15 minutes by default, and can be used
  only once. Discovered Page tokens are encrypted in a short-lived server-side
  session, never sent to the browser, and destroyed immediately after Page
  selection.

## Environment

```dotenv
META_APP_ID="your-app-id"
META_APP_SECRET="your-app-secret"
META_WEBHOOK_VERIFY_TOKEN="a-long-random-verification-token"
META_GRAPH_API_VERSION="v25.0"
META_GRAPH_API_BASE_URL="https://graph.facebook.com"
META_GRAPH_API_TIMEOUT="10"
META_OAUTH_DIALOG_URL="https://www.facebook.com/v25.0/dialog/oauth"
META_OAUTH_REDIRECT_URI="https://api.example.com/api/integrations/facebook/oauth/callback/"
META_OAUTH_FRONTEND_REDIRECT_URL="https://app.example.com/settings/facebook"
META_OAUTH_STATE_TTL="900"
META_OAUTH_SCOPES="pages_show_list,pages_manage_metadata,leads_retrieval"
INTEGRATION_ENCRYPTION_KEY="a-valid-fernet-key"
```

Generate the encryption key once and keep it in the deployment secret store:

```text
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not rotate this key without first re-encrypting stored Page tokens. In local
development only, an omitted value falls back to a key derived from Django's
`SECRET_KEY`.

## Meta configuration

1. Create a Meta Business app and add the Webhooks/Lead Ads products.
2. Add the exact OAuth redirect URL from `META_OAUTH_REDIRECT_URI` to the Meta
   app's valid OAuth redirect URIs.
3. Configure this HTTPS webhook callback URL:
   `https://<api-host>/api/integrations/facebook/webhook/`.
4. Enter the exact value configured as `META_WEBHOOK_VERIFY_TOKEN`.
5. Request the permissions Meta requires for Page discovery, Page webhook
   subscription, and lead retrieval. At the time this connector was built,
   those commonly include `pages_show_list`, `pages_manage_metadata`, and
   `leads_retrieval`; production access is subject to Meta App Review and
   business verification.
6. Run a Celery worker connected to the configured broker.

To use Messenger intake, add `pages_messaging` to `META_OAUTH_SCOPES`, add the
Messenger product/use case to the Meta app, and complete any required App
Review. Existing Lead Ads-only deployments can keep the default scope above.
After changing scopes, reconnect or reauthorize each Page so its token carries
the new permission.

The Graph API version is explicit because Meta versions and permission rules
change. Upgrade `META_GRAPH_API_VERSION` only after running the provider tests
against a Meta test Page.

## Customer self-service connection

An organization administrator opens `/settings/facebook` and selects
**Connect with Facebook**. The application then performs this flow:

1. `POST /api/integrations/facebook/oauth/start/` creates a tenant-owned,
   expiring authorization session and returns the Meta authorization URL.
2. Meta returns to `GET /api/integrations/facebook/oauth/callback/`. The
   backend validates the signed state, exchanges the code, discovers managed
   Pages, and redirects back to the CRM settings page.
3. The settings page reads the sanitized Page list from
   `GET /api/integrations/facebook/oauth/sessions/<session-id>/`.
4. The administrator chooses Pages and submits
   `POST /api/integrations/facebook/oauth/sessions/<session-id>/select/` with
   `{"page_ids": ["..."]}`. Each Page is validated, subscribed to `leadgen`,
   and stored with its token encrypted at rest.

The callback is public because Meta calls it, but it cannot select a tenant
from query parameters alone: the signed state binds the session, organization,
and initiating administrator. Session lookup and Page selection remain
tenant-scoped and administrator-only.

## Manual operations fallback

Only an organization administrator may call this endpoint:

```text
POST /api/integrations/facebook/pages/
Authorization: Bearer <organization JWT>
Content-Type: application/json
```

```json
{
  "page_access_token": "<page-token>",
  "token_expires_at": "2026-10-01T00:00:00Z"
}
```

The Page id and name are read from Meta rather than trusted from the request.
A Facebook Page can belong to only one CRM organization. List or disconnect
pages with:

```text
GET /api/integrations/facebook/pages/
DELETE /api/integrations/facebook/pages/<connection-id>/
```

## Messenger private-message intake

Messenger is opt-in per Page. From `/settings/facebook`, an organization
administrator selects **Enable Messenger** on a connected Page. The backend
subscribes that Page to both `leadgen` and `messages` and records the setting
locally. Disabling Messenger stops private-message intake without changing the
Page's Lead Ads connection.

An administrator can also enable an immediate first-response message and edit
its text per Page. The template supports only `{{ page_name }}` and
`{{ organization_name }}`. The rendered text is snapshotted when the inbound
message is accepted, so later configuration changes cannot alter a queued
reply.

The webhook and worker flow is:

1. Meta sends a signed Page `messages` webhook to the same callback used by
   Lead Ads.
2. The API validates `X-Hub-Signature-256`, resolves the Page connection, and
   persists a message ledger entry before dispatching a durable
   `facebook.process_messenger_message` job. If first-response messaging is
   enabled, it also persists an independent `facebook.send_messenger_reply`
   job and dispatches it before lead processing, without waiting for research.
3. The conversation identity is `<page-id>:<page-scoped-user-id>`. The first
   message creates a `facebook_messenger` SDR intake and passes through shared
   research, qualification, routing, and CRM handoff.
4. Later messages from the same Page-scoped user append to the existing CRM
   lead and record a channel-message lifecycle event instead of creating
   duplicate leads.

Text and attachment types are retained for qualification and audit. Remote
attachment URLs are deliberately not downloaded. Echo messages sent by the
Page are ignored, and Meta message IDs provide idempotency across retries.

The automatic reply uses Meta's `/<page-id>/messages` Send API with the
Page-scoped sender ID as the recipient and `messaging_type=RESPONSE`. One reply
is allowed per Page and PSID conversation. A delayed job older than Meta's
standard 24-hour response window is marked skipped instead of sent. Each reply
records its rendered body, queue/sending/sent/skipped/failed state, attempt
count, provider message ID, failure details, and sent timestamp. Transient
Graph errors use the durable job retry policy; permanent rejections remain in
the audit ledger.

### Sales conversation handoff

Authorized users can continue the conversation directly from the linked CRM
Lead detail page. The conversation API is available at
`GET/POST /api/integrations/facebook/conversations/leads/<lead-id>/`.

- Organization administrators, the Lead creator, the SDR-assigned profile,
  and CRM-assigned sales profiles may read and reply. Other tenant users
  receive `403`, and cross-tenant lookup remains blocked by org filtering and
  RLS.
- The timeline combines inbound customer messages with automatic and manual
  outbound replies in chronological order, including delivery state and the
  sales user who sent each manual reply.
- A manual `POST` requires a UUID `client_request_id` and a text `body`. The
  request ID is unique inside the tenant, so browser retries cannot create a
  second outbound message.
- Manual replies are persisted before the durable
  `facebook.send_messenger_reply` job is dispatched. They use the same Graph
  error classification, retries, Page token, 2,000-character limit, and
  24-hour window enforcement as automatic replies, but do not depend on the
  automatic-reply toggle.

For setup:

1. Add the Messenger product/use case to the Meta Business app.
2. Add `pages_messaging` to `META_OAUTH_SCOPES`, complete App Review as
   applicable, then reconnect Pages.
3. Configure the Page Webhooks `messages` field for the existing callback.
4. Enable Messenger on each intended Page in `/settings/facebook`.
5. Apply `sdr.0011_facebook_messenger_intake` and
   `integrations.0004_facebook_messenger_intake`, followed by
   `integrations.0005_facebook_messenger_auto_reply` and
   `integrations.0006_facebook_messenger_sales_reply`, then run the existing
   automation dispatcher and worker.

## Conversion Leads CRM feedback

An organization administrator configures feedback from `/settings/facebook`
or `GET/PUT /api/integrations/facebook/conversions/`. The access token is a
Conversions API token generated for a Pixel in Meta Events Manager; it is
separate from the Page token used to retrieve Lead Ads submissions. Both are
encrypted at rest and neither is returned by the API.

The integration follows Meta's Conversion Leads CRM payload:

```json
{
  "data": [
    {
      "event_name": "MarketingQualifiedLead",
      "event_time": 1785391200,
      "action_source": "system_generated",
      "user_data": { "lead_id": 1234567890123456 },
      "custom_data": {
        "event_source": "crm",
        "lead_event_source": "BottleCRM"
      }
    }
  ]
}
```

- `RawLead` is queued after a Facebook intake completes.
- `MarketingQualifiedLead` is queued when the SDR qualification band matches
  the administrator's configured bands; `high` is the default.
- `Converted` is queued once when the linked CRM lead first moves to converted.
- Event names are snapshots, so later configuration edits cannot change a
  queued CRM milestone.
- A durable `facebook.send_conversion_event` job retries rate limits and
  transient Graph failures. The event ledger records acceptance count,
  `fbtrace_id`, failure state, and timestamps without storing PII.
- Enabling feedback backfills eligible Facebook intakes from the preceding
  seven days. Older events are rejected locally because Meta does not accept
  Conversion API uploads outside that window.

Use the optional test event code only while verifying the integration in
Events Manager, then clear it for production. The endpoint remains pinned to
`META_GRAPH_API_VERSION` and posts to
`/<pixel-id>/events?access_token=<encrypted-token>`.

Apply `integrations.0003_facebook_conversion_feedback` and run the existing
automation worker/dispatcher before enabling feedback.

## Tenant and failure behavior

The signed webhook contains a Page id but no CRM organization id. A minimal
global routing table maps that Page id to an organization; it contains no Page
token or lead personal data. After routing, PostgreSQL RLS context is set and
the encrypted tenant connection, intake ledger, CRM Lead, and Contact are all
accessed inside that tenant.

If the queue is unavailable, the webhook returns `503` so Meta can retry. If a
worker receives the same `leadgen_id` again, the completed `sdr_lead_intake`
record is replayed instead of creating a second CRM lead.
