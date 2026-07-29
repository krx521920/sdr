# Facebook Lead Ads intake

The Facebook provider receives signed Page `leadgen` webhooks, acknowledges
them quickly, fetches the complete lead from the version-pinned Meta Graph API,
and sends the normalized candidate through the same tenant-scoped SDR pipeline
used by website forms.

## Delivered scope

- Organization administrators can connect a Facebook Page with a Page access
  token. The backend validates the token against Meta and subscribes the Page
  to the `leadgen` webhook field.
- Page tokens are encrypted at rest and never returned by the API.
- The public webhook supports Meta's verification challenge and validates
  `X-Hub-Signature-256` against the raw request body.
- Lead events are queued in Celery, retried with exponential backoff for Graph
  rate limits and transient failures, and deduplicated by Meta `leadgen_id`.
- Standard and custom form questions are preserved. Standard identity and
  company fields are mapped into the shared SDR domain before scoring and CRM
  handoff.

This slice intentionally uses an administrator-supplied Page token. The
customer-facing Meta Login/OAuth selection screen is a separate UI/onboarding
slice; it can call the same connection service after obtaining Page tokens.

## Environment

```dotenv
META_APP_ID="your-app-id"
META_APP_SECRET="your-app-secret"
META_WEBHOOK_VERIFY_TOKEN="a-long-random-verification-token"
META_GRAPH_API_VERSION="v25.0"
META_GRAPH_API_BASE_URL="https://graph.facebook.com"
META_GRAPH_API_TIMEOUT="10"
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
2. Configure this HTTPS callback URL:
   `https://<api-host>/api/integrations/facebook/webhook/`.
3. Enter the exact value configured as `META_WEBHOOK_VERIFY_TOKEN`.
4. Request the permissions Meta requires for Page discovery, Page webhook
   subscription, and lead retrieval. At the time this connector was built,
   those commonly include `pages_show_list`, `pages_manage_metadata`, and
   `leads_retrieval`; production access is subject to Meta App Review and
   business verification.
5. Run a Celery worker connected to the configured broker.

The Graph API version is explicit because Meta versions and permission rules
change. Upgrade `META_GRAPH_API_VERSION` only after running the provider tests
against a Meta test Page.

## Connect a Page

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
