# SDR growth analytics

The SDR growth report turns the existing intake, qualification, routing, CRM,
response, and nurture audit records into one organization-scoped weekly review.
It does not copy those facts into a second analytics table, so operational state
and reported state cannot drift apart.

## Funnel contract

Every funnel stage is a strict subset of the previous stage:

```text
Received
   -> Processed
      -> MQL
         -> Sales handoff
            -> SQL
```

- **Received**: an SDR intake created during the selected period.
- **Processed**: the intake completed the shared SDR pipeline.
- **MQL**: a completed intake with a `high` or `medium` qualification band.
- **Sales handoff**: an MQL with an assigned sales profile and a linked CRM lead.
- **SQL**: a sales-handoff MQL whose linked CRM lead is `converted`.

This progressive definition prevents unrelated CRM conversions from making a
later stage larger than an earlier stage. Source performance uses the same
cohort and definitions, so its MQL and SQL rates reconcile with the headline
funnel.

## Reporting window and attribution

The endpoint accepts `7`, `30`, or `90` days and compares headline counts with
the immediately preceding window of equal length. Daily volume is attributed to
the intake arrival date.

Nurture engagement is attributed to messages sent during the selected period.
Delivery, open, click, reply, and positive-reply outcomes that arrive later are
reported against those sends. A/B metrics use the immutable variant stored on
each delivery rather than the sequence's current configuration.

First-response SLA measures sent acknowledgement-email deliveries from intake
creation to provider handoff. It intentionally does not mix internal sales
notifications or provider-specific Messenger replies into the email SLA.

## Recommendations

The report returns deterministic, auditable recommendations rather than asking
a model to interpret tenant data. It identifies the largest progressive funnel
drop, the strongest source after a minimum sample, low A/B sample size, open rate
below 40%, reply rate below 8%, and acknowledgement SLA attainment below 90%.
These are optimization prompts, not statistically significant experiment
claims.

## API and administration

Organization administrators can open `/settings/sdr-analytics` or call:

```text
GET /api/sdr/analytics/funnel/?days=30
```

The response contains period metadata, current/prior KPIs, progressive funnel
rows, daily trend points, source performance, nurture engagement and variants,
email acknowledgement SLA, recommendations, and metric definitions. The view
requires the authenticated organization context and filters every contributing
query by that organization.

No database migration is required for this reporting layer.
