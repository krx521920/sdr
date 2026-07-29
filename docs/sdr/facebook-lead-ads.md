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

## Tenant and failure behavior

The signed webhook contains a Page id but no CRM organization id. A minimal
global routing table maps that Page id to an organization; it contains no Page
token or lead personal data. After routing, PostgreSQL RLS context is set and
the encrypted tenant connection, intake ledger, CRM Lead, and Contact are all
accessed inside that tenant.

If the queue is unavailable, the webhook returns `503` so Meta can retry. If a
worker receives the same `leadgen_id` again, the completed `sdr_lead_intake`
record is replayed instead of creating a second CRM lead.
