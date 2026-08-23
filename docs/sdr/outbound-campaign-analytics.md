# Outbound Campaign analytics

The SDR Outbound workspace reports lifetime performance for the selected
Campaign at three levels: the full Campaign cohort, ICP segments, and individual
email steps with A/B variants.

## Attribution

Email outcomes are joined through:

`Campaign → Prospect → Intake → Nurture enrollment → Delivery`

This deliberately does not attribute by the Campaign's currently selected
Sequence alone. A Sequence can be reused or changed over time, while the
prospect-to-intake relationship remains the durable source of truth and prevents
results from another Campaign leaking into the report.

WhatsApp and LinkedIn outcomes use each provider record's direct Campaign and
Prospect relationships. Email and channel metrics remain separate because a
WhatsApp read or accepted LinkedIn invitation is not an email open and the
channels have different delivery semantics.

The SDR analytics service obtains WhatsApp and LinkedIn metrics through the
SDR-owned provider ports. Concrete integration models remain inside the
`integrations` application and are registered as adapters during Django startup.

## Metrics

- **Prospects**: every cleaned prospect currently owned by the Campaign.
- **Promoted**: prospects successfully released into the SDR intake pipeline.
- **MQL**: promoted prospect with a completed intake and a `high` or `medium`
  qualification band.
- **Sales handoff**: MQL assigned to a sales profile and linked to a CRM Lead.
- **SQL**: sales-handoff MQL whose CRM Lead status is `converted`.
- **Sent email**: nurture delivery in `sent` state with a `sent_at` timestamp.
- **Opened, clicked, replied, bounced, complained**: unique delivery rows with
  the corresponding first-event timestamp. Rates use sent emails as denominator.
- **WhatsApp sent, delivered, read**: message rows with the corresponding
  timestamp, so later provider states still count as sent.
- **LinkedIn queued, sent, failed, skipped**: invitation audit rows; sent means
  LinkedIn accepted the request and returned an invitation identifier.

Industry and country ICP tables show up to the ten largest segments. Blank
values are grouped under `Unknown`. Step analytics include both configured steps
with zero sends and historical delivery positions that may no longer exist in
the current Sequence.

## API

`GET /api/sdr/outbound/campaigns/<campaign_id>/analytics/`

The endpoint is organization-admin only, enforces organization scope, and
returns `404` rather than exposing the existence of another tenant's Campaign.
