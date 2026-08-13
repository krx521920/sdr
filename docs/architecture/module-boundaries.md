# Module boundaries

BottleCRM remains a modular Django monolith. New SDR behavior is introduced
through explicit bounded contexts so the existing CRM can continue to run
while its large shared modules are reduced incrementally.

## Runtime flow

```text
Facebook / website / LinkedIn / email
                  |
                  v
            integrations
                  |
                  v
        sdr ingestion pipeline
          |      |      |
          v      v      v
       enrich  score  route
                  |
                  v
              CRM handoff
                  |
                  v
        existing CRM Django apps

automation supplies tenant-aware events, jobs, retries, and workflows to all
application modules without owning CRM business rules.
```

## Ownership

### `sdr`

Owns normalized lead candidates, deduplication, enrichment, qualification,
routing decisions, and the validated handoff into CRM records. Its `domain`
package is framework-independent.

### `integrations`

Owns external API clients, credentials, webhook verification, cursor handling,
rate limiting, and conversion of provider payloads into `sdr.domain` values.
It must not write CRM models directly.

### `automation`

Owns generic event envelopes, idempotent job requests, retry policy, scheduling,
and workflow execution. It must not contain lead scoring or routing rules.

### Existing CRM apps

`accounts`, `contacts`, `leads`, `opportunity`, `tasks`, `cases`, `invoices`,
and `orders` remain record systems during the incremental migration. Only SDR
CRM adapters may translate domain values into these Django models.

## Dependency rules

1. `sdr.domain` imports no Django, provider, automation, or legacy CRM modules.
2. `integrations` may depend on `sdr.domain` and `sdr.ports`; `sdr` never imports
   provider implementations.
3. All cross-module messages include `org_id`; background jobs additionally
   require an idempotency key.
4. Provider payloads are normalized before deduplication, scoring, or routing.
5. New SDR code must not import legacy serializers or views.
6. Tenant writes pass through one CRM handoff port and are never performed by
   an external provider adapter.

## Incremental migration

1. Add provider adapters and persist raw intake events.
2. Implement CRM adapters for deduplication and handoff.
3. Add workflow persistence, retries, and a dead-letter queue.
4. Move tenancy, identity, audit, notifications, and files out of `common` only
   after a migration plan preserves Django app labels, permissions, and tables.
5. Split large route/view files behind application services without changing
   public API contracts.

The durable execution work in step 3 is implemented by the tenant-scoped job
ledger described in [durable-automation-jobs.md](durable-automation-jobs.md).
Country/source/qualification assignment is implemented inside the `sdr`
boundary as described in [sdr-routing.md](sdr-routing.md).
Public company research and AI-assisted qualification remain behind the SDR
ports described in [lead-inspector.md](lead-inspector.md).
Provider protocol translation, encrypted BYOK credentials, and failover are
isolated behind the [SDR model gateway](model-gateway.md).
Immediate website acceptance, lifecycle audit, acknowledgement, and sales
handoff delivery are described in [lead-response-loop.md](lead-response-loop.md).
Scheduled follow-up, deterministic A/B variants, signed open/click tracking,
one-click unsubscribe, tenant suppression, signed SES delivery feedback,
reply outcomes, and automatic CRM-state exits are described in
[lead-nurturing.md](lead-nurturing.md).
Signed email receipt, mailbox-purpose routing, automatic reply capture, and new
email lead normalization are described in [inbound-email-sdr.md](inbound-email-sdr.md).
Meta Lead Ads and Messenger private-message ingestion, durable in-window first
responses, assigned-sales conversation handoff, plus PII-free Conversion Leads
CRM feedback are described in
[facebook-lead-ads.md](../sdr/facebook-lead-ads.md).
Progressive MQL-to-SQL funnel reporting, acquisition-source quality, nurture
A/B outcomes, first-response SLA, and deterministic weekly optimization prompts
are described in [sdr-growth-analytics.md](sdr-growth-analytics.md).
Outbound channel execution, provider readiness, prospect-source clients, and
channel-specific analytics are resolved through the SDR-owned runtime ports in
`sdr.provider_ports`. The `integrations` app registers its Apollo, LinkedIn, and
WhatsApp adapters during Django startup, so SDR services never import provider
models or implementations directly.
ICP campaign management, CSV prospect cleaning, tenant-wide deduplication, and
durable outbound-to-CRM promotion are described in
[outbound-prospecting.md](outbound-prospecting.md).
Official WhatsApp Cloud API template delivery, encrypted sender credentials,
durable campaign jobs, and signed delivery receipts are described in
[whatsapp-outbound.md](../sdr/whatsapp-outbound.md).
Official, partner-gated LinkedIn connection invitations are described in
[linkedin-outbound.md](../sdr/linkedin-outbound.md).
Official Feishu Bitable field discovery and idempotent research-result upserts
are implemented by an `integrations` research-result sink registered behind the
SDR runtime port, as described in
[feishu-base-research-sync.md](../sdr/feishu-base-research-sync.md).
