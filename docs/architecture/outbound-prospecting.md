# SDR outbound prospecting

The outbound workspace adds a tenant-owned campaign and prospect-list boundary
before a researched contact is allowed into the shared SDR pipeline. This keeps
unreviewed list data separate from active CRM leads while reusing the existing
research, qualification, routing, CRM handoff, audit, and automation machinery
after promotion.

## Runtime flow

```text
ICP campaign
    -> CSV prospect list
       -> normalize + validate
       -> deduplicate inside file
       -> deduplicate across tenant campaigns
       -> deduplicate against existing CRM leads
          -> ready prospect
             -> guarded campaign run + daily release cap
                -> durable promotion job
                -> research -> qualify -> route -> CRM handoff
                   -> explicit outbound nurture sequence
```

Campaigns record the ICP brief, intended contact channels, selected email
sequence, daily release limit, lifecycle status, run number, and optional owner.
Prospects retain researched contact and company fields,
source evidence, list status, promotion attempts, safe error details, and the
resulting SDR intake reference.

## CSV contract

Imports accept at most 500 rows and 1 MB of text. `company_name` is required;
each row must also contain at least one of `email`, `phone`, `linkedin_url`, or
`website`.

Supported headers are:

```text
first_name,last_name,email,phone,job_title,linkedin_url,company_name,website,industry,country,source_url,notes
```

Email addresses are lowercased, whitespace is normalized, common bare domains
receive an HTTPS scheme, and recognized country names become ISO codes. Invalid
rows are returned with row/field errors and do not block valid rows.

The organization-wide prospect fingerprint prefers email, then LinkedIn URL,
then normalized phone, then company website plus company name. The importer
checks that fingerprint inside the CSV and all existing outbound campaigns. It
also checks matching identities in the tenant's CRM leads. Cross-tenant data is
never part of a duplicate lookup.

## Durable promotion and messaging safety

Promotion persists `sdr.process_outbound_prospect` before broker dispatch and
uses a prospect plus campaign-run idempotency key. Retryable SDR
pipeline failures remain auditable and dead-letter jobs can be replayed from the
prospect action without creating another intake. The intake source is
`outbound`, so source routing and analytics continue to work without a second
pipeline.

Outbound promotion never sends the inbound acknowledgement template. Imported
contacts are also excluded from general automatic nurture matching. Launching
a campaign requires an enabled email sequence with a sender, at least one step,
and an explicit `outbound` source. Tenant suppression and the existing delivery
controls still apply.

Launch creates a new run and releases no more than `daily_send_limit` eligible
email prospects per local day. A 15-minute reconciliation task refills active
campaigns after the day changes. Pausing a campaign invalidates queued work from
the old run and pauses its active nurture enrollments; resuming creates a new
run and only resumes enrollments that were paused by the campaign control. This
keeps stale broker jobs from contacting a prospect after an operator pauses the
campaign.

## API and administration

Organization administrators use `/settings/sdr-outbound` or:

```text
GET/POST  /api/sdr/outbound/campaigns/
GET/PATCH /api/sdr/outbound/campaigns/<campaign-id>/
POST      /api/sdr/outbound/campaigns/<campaign-id>/action/
GET       /api/sdr/outbound/campaigns/<campaign-id>/prospects/
POST      /api/sdr/outbound/campaigns/<campaign-id>/prospects/import/
POST      /api/sdr/outbound/prospects/<prospect-id>/action/
```

Campaign actions are `launch`, `pause`, `retry_failed`, `complete`, and
`archive`. Prospect actions are `promote`, `disqualify`, and `restore`. Apply
migrations `sdr.0012_outbound_prospecting` and
`sdr.0013_outbound_campaign_execution`; both outbound tables are protected by
PostgreSQL RLS.
