# SDR lead response loop

The response loop separates safe intake acceptance from slow enrichment and
external delivery. A website caller receives an acknowledgement after the raw
lead and its durable job are committed, rather than waiting for website
research or an AI provider.

## Runtime flow

```text
website request
    |
    v
persist intake + durable job --> 202 Accepted
    |
    v
deduplicate -> research -> qualify -> route -> CRM handoff
    |
    +--> acknowledgement email job
    +--> assigned-sales in-app notification job
    +--> Feishu custom-bot notification job
             |
             +--> retry -> dead letter -> administrator replay
```

Facebook continues to acknowledge Meta quickly and fetch the full lead inside
its existing durable job. Once it enters the shared SDR pipeline, it schedules
the same response policy as a website lead.

## Idempotency and audit

- Intake uniqueness remains `(organization, source, source_record_id)`.
- Website processing uses `intake:<intake-id>` as its automation key.
- Each delivery owns a `sdr_lead_delivery` row and a
  `delivery:<delivery-id>` automation key.
- Sent or skipped deliveries are terminal and safe under broker redelivery.
- `sdr_lead_lifecycle_event` records stable received, queued, processing,
  qualification, assignment, CRM, response, and failure milestones.

The intake, settings, lifecycle, and delivery tables are organization-owned
and protected by PostgreSQL RLS.

## Administration

Organization administrators configure and monitor the loop at
`/settings/automation` or through:

```text
GET/PUT/PATCH /api/sdr/response-settings/
GET           /api/sdr/intakes/
GET           /api/sdr/intakes/<intake-id>/
```

Acknowledgement templates support `first_name`, `last_name`, `company_name`,
`organization_name`, `qualification_band`, and `qualification_score` Django
template variables. Email is disabled by default so a deployment cannot send
customer messages until an administrator reviews the sender and copy.

Feishu delivery uses an encrypted custom-bot webhook. Configuration accepts
only HTTPS webhook URLs on `open.feishu.cn` or `open.larksuite.com` with the
current `/open-apis/bot/v2/hook/` path. The API returns only a short hint, never
the webhook or ciphertext. See the official [custom bot guide](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=en-US).

## Operations

Run Celery worker and Beat. The tenant-aware `sdr.reconcile_response_jobs`
task runs every minute for recently completed intakes, which closes the small
failure window between CRM completion and downstream job creation.

Apply migration `sdr.0005_lead_response_loop` and configure:

```dotenv
SDR_FEISHU_TIMEOUT_SECONDS="5"
```
