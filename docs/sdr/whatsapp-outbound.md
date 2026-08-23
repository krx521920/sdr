# WhatsApp outbound campaigns

SDR outbound campaigns can execute approved WhatsApp Business Platform template
messages without requiring an email address or nurture sequence. The provider
boundary uses Meta's official Cloud API and does not automate a consumer
WhatsApp session.

## Deployment configuration

Set `META_APP_SECRET` so webhook signatures can be verified and set a dedicated
`WHATSAPP_WEBHOOK_VERIFY_TOKEN`. `META_GRAPH_API_VERSION`,
`META_GRAPH_API_BASE_URL`, and `META_GRAPH_API_TIMEOUT` are shared with the
existing Meta integration. Production must configure an independent
`INTEGRATION_ENCRYPTION_KEY` before storing the system-user access token.

Apply these migrations:

- `sdr.0015_sdroutboundcampaign_whatsapp_template_language_and_more`
- `integrations.0007_whatsappphoneroute_whatsappbusinessconnection_and_more`
- `integrations.0008_whatsapp_rls`

Configure Meta's WhatsApp webhook URL as:

`https://<api-host>/api/integrations/whatsapp/webhook/`

Subscribe the WhatsApp Business Account to the `messages` webhook field. The
endpoint accepts delivery statuses only after verifying `X-Hub-Signature-256`.

## Tenant connection

An organization administrator configures its sender with:

```http
PUT /api/integrations/whatsapp/connection/
Content-Type: application/json

{
  "phone_number_id": "123456789",
  "business_account_id": "987654321",
  "display_phone_number": "+1 555 123 4567",
  "access_token": "<system-user-token>",
  "is_active": true
}
```

The token is encrypted at rest and never returned by the API. `GET` on the same
endpoint exposes only its final-character hint, connection state, timestamps,
and aggregate delivery counts.

## Campaign execution

Create or update a paused/draft Campaign with:

```json
{
  "channels": ["whatsapp"],
  "whatsapp_template_name": "industrial_intro",
  "whatsapp_template_language": "en_US"
}
```

The template must already be approved in WhatsApp Manager. Imported phone
numbers are normalized to 8-15 international digits. Launch requires an active
tenant connection and an approved template name. Each prospect and Campaign
run produces at most one `WhatsAppMessage` and one durable
`whatsapp.send_campaign_message` automation job.

Before calling Meta, the worker rechecks the Campaign status, run number, and
channel selection. Paused or superseded runs are marked `skipped`; successful
requests store Meta's `wamid`. Signed webhook events advance the audit state
through `sent`, `delivered`, `read`, or `failed`.

LinkedIn execution is implemented separately through the partner-only official
Invitations API. See [linkedin-outbound.md](linkedin-outbound.md). It does not
use browser automation, cookies, or personal-account session simulation.
