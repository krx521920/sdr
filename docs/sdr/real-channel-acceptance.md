# Real channel acceptance

Use this runbook only in a dedicated organization such as `SDR Integration
Sandbox`. Never use a production campaign, customer list, employee account, or
unconsented recipient as a connectivity test.

> Current release gate: do not run a remote acceptance step until the worker
> enforces the dedicated organization, exact provider test object, one-shot
> approval, consent/DNC decision, quota or credit reservation, and global kill
> switch immediately before the provider call. A settings-page configuration
> check is local only and must never be presented as proof that a channel is
> verified or production-ready.

The application now has a fail-closed execution ledger with environment,
organization, and channel kill switches; organization-wide and per-channel
daily limits; exact test-target HMACs; single-use approvals; and durable
`RESERVED`, `SENDING`, `ACCEPTED`, `DELIVERED`, `FAILED`, and `UNKNOWN` states.
Apollo, Feishu Base, Email, WhatsApp, and LinkedIn network clients reject an
unguarded production call before network I/O. This is deliberately a hard
lock, not evidence that a channel is ready: legacy jobs remain blocked until
their queue payload carries a valid execution-request ID created from the
exact target, payload hash, action, approval, and reserved units. The
test-settings bypass exists only to exercise legacy unit tests and must never
be copied into a deployment setting.

Apollo is the first provider whose application-level enqueue contract is
complete. **Settings → SDR Outbound** now uses two explicit steps: the first
request returns only the exact search or enrichment intent, and the second
request accepts a separately issued one-time approval UUID. A search stores
only an encrypted provider identifier, its HMAC, and a non-identifying local
label; each selected enrichment needs its own approval and enters the canonical
Person/Evidence import ledger. This has been verified with mock providers and
real PostgreSQL concurrency tests, but it is not a successful remote Apollo
acceptance and no real credential has been exercised.

Apollo recovery is deliberately conservative. A stale `RESERVED` request is a
proven pre-I/O failure and is refunded; a stale `SENDING` request becomes
charged `UNKNOWN` and is never automatically replayed. Celery runs this
organization-scoped recovery every five minutes. An administrator must resolve
`UNKNOWN` from sanitized provider evidence, and any later attempt requires a
new approval and idempotency key.

Feishu Base now has its own exact intent/reservation/job/settlement contract for
schema validation, one-record research-result upsert, remote deletion, and a
bounded Person-import snapshot. Each
operation uses the internal target `feishu-base:{connection_id}`, one mutation
at most, and a single-attempt durable job. Remote record IDs are encrypted and
represented outside the provider client only by an organization-bound HMAC and
safe label. An uncertain call remains charged `UNKNOWN` and is never retried
automatically. If an accepted create cannot recover its remote record ID, the
ledger requires manual reconciliation and the operator must locate and erase
the row in Feishu; there is not yet a safe record-ID recovery workflow. The
legacy automatic sink and custom-bot webhook remain disabled in production.
The import action reads at most 500 rows, encrypts its one-time mapping, stores
only destination-scoped HMAC record keys, and produces a reviewable zero-or-more
row Person-import preview. It never commits Person or Evidence automatically.
Provider read, preview persistence, quota settlement, and delivery are one
transactional state transition; a crash is reconciled as consumed `UNKNOWN`
without replaying the Base read.
Email now has an application-level two-stage contract for acknowledgement and
nurture deliveries. In production-safe mode the first stage persists a pending
delivery without creating a job; an organization administrator can then view
the exact target/payload hashes, consume a matching one-time approval, and
enqueue a single-attempt job bound to the delivery UUID. `SENDING` and
`UNKNOWN` requests cannot be replayed, while `ACCEPTED` is recovered through
local convergence only. This has been verified with mock providers and real
PostgreSQL concurrency tests, but it is not a successful remote email
acceptance and no real credential or recipient has been exercised.

WhatsApp now uses the same application-level boundary. A production-safe
campaign creates a reviewable `PENDING` message and no provider job. The admin
queue exposes only local UUIDs, durable states, and exact target/payload
fingerprints; recipient, template, Meta message ID, provider error, and payload
snapshot remain server-side. A matching one-time approval reserves one unit and
creates one single-attempt job whose idempotency key is the message UUID. The
worker rechecks campaign state, consent/DNC, connection state, immutable
snapshot, kill switches, quota, and test-target scope immediately before I/O.
A deterministic rejection is safely released, whereas transport ambiguity,
5xx/408/429, or a local-persistence gap becomes consumed `UNKNOWN` and is never
automatically replayed. Only a signed `delivered`/`read` webhook advances an
accepted request to `DELIVERED`; manual UNKNOWN resolution converges the local
message without another provider call. The contract has been verified with a
mock client plus real PostgreSQL trigger and same-message concurrency tests;
this is not a successful remote Meta acceptance and no real credential or
recipient has been exercised. LinkedIn still has only the final provider I/O
lock or partial legacy plumbing, so its remote acceptance remains blocked even
if credentials can be saved locally.

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

1. Prove CSV and internal-CRM ingestion against the unified Person/Evidence
   ledger without any external provider call.
2. Run the local-only checks for Apollo, WhatsApp, and LinkedIn from **Settings →
   SDR Outbound**. These checks decrypt stored configuration but make no provider
   request and create no task, message, invitation, prospect, or campaign record.
3. Run the smallest Apollo search allowed by the account contract. Do not run
   person enrichment as a connection test or without an explicit credit budget.
   Search and enrichment are separate charged operations: approve and account
   for the search first, persist only its bounded candidate receipt, and then
   approve each selected person enrichment separately. A single `SENDING`
   execution request must never authorize both operations or multiple HTTP
   calls.
4. After configuring an active connection, allow-list the exact internal target
   shown by the first-stage intent and issue a separate one-time approval for
   each action. Validate the dedicated Base schema, write one synthetic record,
   run one bounded Person-import snapshot into the shared preview ledger, and
   then delete the same synthetic record. Review the import batch before its
   separate commit; the read action must not create Person or Evidence by
   itself. These actions are technically eligible for remote acceptance, but
   have not been exercised with real Feishu credentials or a dedicated
   synthetic Base yet.
5. Run an AI request using only the fixed synthetic input and the agreed budget.
6. Verify email with the dedicated company-controlled mailbox and a test message
   whose sender and recipient are both allow-listed.
7. Validate WhatsApp phone metadata without calling the messages endpoint, then
   perform one approved-template send to the exact consented test recipient.
8. Treat LinkedIn last: a local check does not prove Invitations API permission.
   Perform one invitation only with an approved Partner account and exact test
   recipient.
9. Keep WeChat and WeCom disabled until an official provider, callback security,
   privacy review, and exact test-object gate have all been implemented.

For every remote step, record the organization, operator, UTC timestamp,
provider, test-object identifier, expected cost, result code, and any provider
request ID. Do not record credentials, generated message content containing PII,
or raw provider error bodies.

## Stop conditions

Stop immediately on an unexpected recipient, write, credit charge, permission
scope, provider environment, or response containing sensitive data. Revoke the
affected test credential, retain the sanitized audit event, and investigate
before retrying. Never convert a failed remote test into an automatic retry.
