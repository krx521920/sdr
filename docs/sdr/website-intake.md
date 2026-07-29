# Website lead intake

The first SDR vertical slice accepts a server-to-server website form, retains
the original submission, deduplicates it, enriches known fields, assigns an
explainable qualification score, routes it to sales, and writes the resulting
Lead and Contact into BottleCRM.

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
Submitting it again returns the completed intake instead of creating a second
lead. A processing attempt older than five minutes can be reclaimed safely.

## Processing order

1. Store the raw request in `sdr_lead_intake`.
2. Match an active Lead by tenant-scoped email or phone.
3. Enrich business-email and website-domain information.
4. Calculate a transparent 0-100 baseline score.
5. Select the least-loaded active sales profile, falling back to an admin.
6. Create or update the Lead and Contact in one database transaction.
7. Mark the intake completed with its CRM IDs, score, band, and assignment.

Failures are retained with an error and attempt count so a retry does not lose
the original submission.

