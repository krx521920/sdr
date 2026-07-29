# Website lead intake

The website channel accepts a server-to-server form and persists it before
returning. Durable background jobs then deduplicate it, enrich known fields,
assign an explainable qualification score, route it to sales, write the Lead
and Contact, and run the configured customer/sales response policy.

## Endpoint

```text
POST /api/sdr/intake/website/
Token: <organization-api-key>
Content-Type: application/json
```

The legacy URL `/api/leads/create-from-site/` points to the same pipeline.
Body-level API keys are no longer accepted. Do not expose the organization API
key in browser JavaScript; submit through the website's trusted server or
serverless backend.

## Request

```json
{
  "source_record_id": "form-submission-20260729-001",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "email": "ada@example.com",
  "phone": "+44 20 7946 0958",
  "company_name": "Example Ltd",
  "job_title": "VP Sales",
  "message": "We want to automate inbound lead qualification and routing.",
  "page_url": "https://example.com/contact",
  "utm_source": "google",
  "utm_medium": "cpc",
  "utm_campaign": "enterprise-sdr"
}
```

`source_record_id` is the idempotency key within one organization and source.
Submitting it again returns the same intake and processing job instead of
creating a second lead. A completed replay includes the existing CRM lead id.

The normal response is `202 Accepted`:

```json
{
  "intake_id": "...",
  "job_id": "...",
  "status": "received",
  "lead_id": null,
  "replayed": false,
  "status_url": "/api/sdr/intakes/<intake-id>/"
}
```

Poll `GET /api/sdr/intakes/<intake-id>/` when the submitting system needs the
completed CRM, qualification, assignment, lifecycle, and delivery result.

## Processing order

1. Store the raw request in `sdr_lead_intake`.
2. Persist the `sdr.process_intake` job and, when enabled, an immediate
   acknowledgement email job.
3. Return `202` without waiting for email, website research, or a model provider.
4. Match an active Lead by tenant-scoped email or phone.
5. Enrich, research, and calculate a transparent 0-100 qualification score.
6. Apply tenant routing and create or update the CRM Lead and Contact.
7. Record lifecycle milestones and mark the intake completed.
8. Persist independent sales notification jobs; acknowledgement scheduling is
   idempotently reconciled in case it was not created during acceptance.

Broker, model, email, and notification failures are retained with attempt
history and exponential backoff. Exhausted jobs enter the dead-letter ledger
and can be replayed without duplicating the intake, CRM record, or delivery.
