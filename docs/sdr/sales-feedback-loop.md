# SDR sales feedback loop

The sales feedback loop records the downstream verdict for each SDR handoff and
turns enough reviewed outcomes into privacy-safe calibration context for future
AI qualification.

## Sales workflow

Sales-enabled users can review an SDR-originated CRM lead when they are either
the intake assignee or directly assigned to the CRM lead. Organization admins
can review any handoff in their organization.

`GET /api/sdr/sales-feedback/leads/<lead_id>/` returns the latest SDR intake,
the original qualification metadata, the current feedback, and controlled
decision/reason choices. `PUT` creates or updates the verdict:

- decision: accepted, rejected, or recycle/nurture;
- a controlled reason (required for rejected and recycled leads);
- lead quality and SDR handoff satisfaction scores from 1 to 5;
- an optional note, required when the controlled reason is `other`.

The first submission freezes the qualification score, band, provider, model,
and prompt version. Editing the verdict does not rewrite these snapshots, so
the analytics continue to measure the model that made the original handoff.

## Analytics and calibration

The SDR growth analytics response includes sales-feedback coverage, acceptance,
average quality/satisfaction, rejection reasons, and outcomes grouped by the
captured qualification band and model version.

Future AI qualification receives aggregate feedback only after at least 10
reviews in the prior 180 days. The context contains sample counts, acceptance
rates by predicted band, and the top controlled rejection reasons. Lead identity
and free-text notes are never sent to the model. The aggregate is advisory;
current lead evidence and the configured ICP remain authoritative.

## Tenant isolation

`sdr_sales_feedback` is organization-scoped and protected by forced PostgreSQL
RLS. API queries also filter by organization and enforce sales assignment before
returning or changing feedback.
