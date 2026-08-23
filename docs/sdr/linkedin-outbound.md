# LinkedIn outbound invitations

SDR Campaigns can send LinkedIn connection invitations through LinkedIn's
official Invitations API. This API is restricted to approved LinkedIn partners;
the implementation does not automate a browser, reuse personal cookies, scrape
sessions, or pretend that an unavailable API action succeeded.

Official references:

- [Invitations API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/communications/invitations)
- [Communication APIs](https://learn.microsoft.com/en-us/linkedin/shared/integrations/communications/overview)
- [Sales Navigator platform access](https://learn.microsoft.com/en-us/linkedin/sales/)

## Configuration

An organization administrator opens **Settings → SDR Outbound**, enters the
official partner API access token, explicitly confirms approved partner access,
and enables Campaign invitations. Tokens are encrypted at rest and only their
last eight characters are returned to the UI.

The deployment controls these optional environment settings:

```dotenv
LINKEDIN_API_BASE_URL=https://api.linkedin.com
LINKEDIN_API_TIMEOUT=10
```

The base URL is not tenant-configurable, preventing a tenant credential from
redirecting requests to an arbitrary host.

## Campaign behavior

A Campaign with the `linkedin` channel can run without an email Sequence, but
launch is blocked unless its organization has an active, confirmed LinkedIn
partner connection. Each eligible Prospect must have a valid email because the
official API supports email invitee URNs; a public profile URL alone is not
converted into a private LinkedIn member identifier.

The optional invitation note supports these variables:

- `{{ first_name }}`
- `{{ last_name }}`
- `{{ full_name }}`
- `{{ company_name }}`
- `{{ job_title }}`

The final personalized note must not exceed 300 characters. A blank note sends
an invitation without the optional message body.

## Delivery and audit

Every Prospect and Campaign run creates at most one `LinkedInInvitation` and
one durable `linkedin.send_campaign_invitation` job. Before calling LinkedIn,
the Worker rechecks Campaign status, run number, selected channel, and connection
state. It records queued, sending, sent, failed, or skipped status; a successful
request stores the `x-linkedin-id` returned by LinkedIn.

HTTP 429 and server errors are retryable. Authentication or authorization
failures are permanent and reported as `linkedin_partner_access_required` so an
operator can correct the entitlement or token rather than repeatedly retrying.
Campaign analytics expose queued, sent, failed, and skipped invitation totals.

## Tenant isolation

`integration_linkedin_connection` and `integration_linkedin_invitation` both
use PostgreSQL row-level security. Application queries also scope by
organization, and model validation prevents cross-tenant Campaign, Prospect, or
connection relationships.
