# Real channel acceptance

Use this runbook only in a dedicated organization such as `SDR Integration
Sandbox`. Never use a production campaign, customer list, employee account, or
unconsented recipient as a connectivity test.

## Security prerequisites

1. Initialize the local deployment key once:

   ```powershell
   .\docker\initialize-integration-encryption-key.ps1
   ```

   The script writes only to the gitignored `.env.docker.local` file and never
   prints the key. Do not replace that key after credentials have been stored;
   key rotation requires a database re-encryption procedure.
2. Recreate the backend, Celery worker, and Celery beat so all three processes
   load the same key.
3. Create a separate integration organization with a company-controlled admin
   mailbox protected by MFA. Do not reuse a personal mailbox or automation
   token.
4. Enter provider credentials only through the authenticated settings pages or
   an approved deployment secret manager. Never paste secrets into chat, Git,
   issue comments, shell arguments, screenshots, or test fixtures.
5. Keep the local frontend and backend bound to `127.0.0.1`. A remote deployment
   must sit behind TLS, authentication, and an access-controlled reverse proxy.

## Test objects

| Provider | Dedicated object | First permitted check | Side-effecting acceptance |
| --- | --- | --- | --- |
| Feishu | Test app, Base, table, and field mapping containing synthetic rows only | Read table fields | Write one synthetic research record, then delete it |
| AI | Allow-listed low-cost model and synthetic company/person prompt | Local key/model check | One fixed, non-PII prompt with a small token cap |
| Apollo | Test account or saved search with narrow country/title/company filters | Local key check | One result page only; no enrichment unless credit use is approved |
| WhatsApp | Meta test phone number, approved test template, and one consented recipient | Local token/phone ID check, then read-only phone metadata | Send one approved template to that recipient only |
| LinkedIn | Approved Partner account and a non-production test identity | Local token and partner-confirmation check | One invitation only after the exact recipient is approved |

## Acceptance order

1. Run the local-only checks for Apollo, WhatsApp, and LinkedIn from **Settings →
   SDR Outbound**. These checks decrypt stored configuration but make no provider
   request and create no task, message, invitation, prospect, or campaign record.
2. Run the existing Feishu field check against the dedicated Base.
3. Run an AI request using only the fixed synthetic input and the agreed budget.
4. Run the smallest Apollo search allowed by the account contract. Do not run
   person enrichment as a connection test.
5. Validate WhatsApp phone metadata without calling the messages endpoint.
6. Perform the single WhatsApp send only after the template, language, and
   recipient have been approved.
7. Treat LinkedIn last: a local check does not prove Invitations API permission.
   Perform a real invitation only with an approved Partner account and recipient.

For every remote step, record the organization, operator, UTC timestamp,
provider, test-object identifier, expected cost, result code, and any provider
request ID. Do not record credentials, generated message content containing PII,
or raw provider error bodies.

## Stop conditions

Stop immediately on an unexpected recipient, write, credit charge, permission
scope, provider environment, or response containing sensitive data. Revoke the
affected test credential, retain the sanitized audit event, and investigate
before retrying. Never convert a failed remote test into an automatic retry.
