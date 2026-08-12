# SDR lead nurturing

Lead nurturing turns completed inbound intakes that are not yet sales-ready into
scheduled, auditable follow-up journeys. It reuses the durable automation ledger
instead of relying on Celery messages as the source of truth.

## Runtime flow

```text
completed SDR intake
        |
        v
active sequences ordered by priority
        |
        +-- source matches (empty = any)
        +-- qualification band matches (empty = any)
        |
        v
create one enrollment per intake
        |
        v
persist step delivery + automation job
        |
        +-- wait until scheduled_for
        +-- validate current lead/sequence state
        +-- render the immutable A/B template snapshot
        +-- send email
        +-- persist result and schedule the next step
```

The first matching sequence wins. Automatic enrollment is opt-in: a sequence
must be both active and explicitly configured for automatic enrollment. No
sequence is created or enabled by default.

## Scheduling and recovery

- Each delivery owns an `sdr_nurture_delivery` record and a durable
  `nurture-delivery:<delivery-id>:dispatch:<resume-count>` automation key.
- Future jobs remain in the database until the normal due-job dispatcher sees
  them. Long delays are not held as broker countdown messages.
- A five-minute reconciler repairs the small transaction-to-scheduling gap and
  never advances past an outstanding step.
- Pausing leaves the next delivery intact. Resuming increments the dispatch
  generation, so an already-completed paused worker job cannot lose the step.
- Converted, closed, or inactive CRM leads stop before the next external send.

Email delivery is at-least-once across a process crash between the external
email provider accepting a message and the database recording success. The
delivery ledger prevents ordinary broker redelivery from sending a completed
step again.

## A/B and engagement metrics

Each step contains variant A and an optional variant B traffic percentage. A
SHA-256 bucket derived from the enrollment and step position assigns a stable
variant. The delivery stores the selected subject and body snapshot, so later
template edits cannot change an already-scheduled message.

Sales can mark a reply as positive, neutral, or negative. A mailbox routed to
SDR also captures replies automatically through the signed inbound-email flow.

Each outgoing email contains an expiring, tenant-bound signed pixel and signed
HTTP(S) redirect links. The public endpoints verify the signature before
setting the tenant RLS context. They never accept an unsigned destination, and
HEAD/prefetch requests do not count as engagement. Plain-text links are also
rewritten, while the HTML alternative is generated from escaped text rather
than treating administrator copy as raw HTML.

Interactions store only a keyed hash of the remote address plus user agent.
Repeated events from the same visitor and target are deduplicated; raw network
identifiers are not retained. Delivery-level first-open/first-click timestamps
power unique open and click rates, while counters retain the deduplicated
interaction volume. Email-client privacy proxies and security scanners can
still affect these metrics, so replies remain the strongest intent signal.

The sequence API reports sent, unique opened, unique clicked, reply and
positive-reply metrics, including per-variant open/click/reply rates.

## SES delivery feedback

Outgoing nurture messages include the SES message tags `sdr_org` and
`sdr_delivery`. The installed `django-ses` backend also exposes the provider
`MessageId` after a successful API call, which is stored as a secondary
correlation key.

AWS SNS posts delivery, bounce, and complaint notifications to:

```text
POST /api/sdr/public/ses-feedback/
```

The endpoint verifies the SNS certificate signature before trusting the tenant
or delivery tags and setting the RLS context. Sanitized events are persisted in
an idempotent provider-event ledger keyed by the SNS message ID. Recipient and
provider-message-ID mismatches are ignored instead of being associated by email
alone.

- Delivery events record the provider-confirmed delivery timestamp.
- Permanent bounces create a `hard_bounce` suppression and cancel nurture.
- Complaints create a `complaint` suppression and cancel nurture.
- Transient bounces remain auditable but do not permanently suppress the email.

The sequence API and administration UI expose delivery, bounce, complaint, and
per-variant bounce metrics. Configure `AWS_SES_CONFIGURATION_SET`, publish its
Delivery/Bounce/Complaint events to an SNS topic, and subscribe the endpoint
above to that topic.

## Unsubscribe and suppression

Every nurture message includes both a visible unsubscribe link and the RFC
8058 headers `List-Unsubscribe` and `List-Unsubscribe-Post`. A signed GET shows
a confirmation page without changing state, which avoids accidental opt-outs
from link scanners. A signed POST performs an idempotent one-click unsubscribe.

One-click requests, explicit inbound-email opt-outs, provider complaints or
hard bounces, and administrator actions share the tenant-owned
`sdr_email_suppression` ledger. An active suppression:

- cancels every active, paused, or completed nurture enrollment for the email;
- prevents a later intake for the same tenant and email from auto-enrolling;
- is checked again immediately before each external send; and
- can be released by an organization administrator without deleting its audit
  timestamps. Releasing a record allows future intakes but does not restart an
  old canceled enrollment.

## Administration

Organization administrators use `/settings/sdr-nurturing` or:

```text
GET/POST       /api/sdr/nurture/sequences/
GET/PUT/PATCH  /api/sdr/nurture/sequences/<sequence-id>/
DELETE         /api/sdr/nurture/sequences/<sequence-id>/
GET            /api/sdr/nurture/enrollments/
POST           /api/sdr/nurture/enrollments/<enrollment-id>/action/
GET/POST       /api/sdr/nurture/suppressions/
DELETE         /api/sdr/nurture/suppressions/<suppression-id>/
GET/POST       /api/sdr/public/nurture/unsubscribe/<signed-token>/
POST           /api/sdr/public/ses-feedback/
```

Supported enrollment actions are pause, resume, cancel, mark replied, and mark
converted. Templates use the same safe simple-variable allow-list as the lead
acknowledgement email.

Apply migrations through `sdr.0010_ses_delivery_feedback`. Configure
`SDR_NURTURE_TRACKING_BASE_URL` as the externally reachable application origin;
signed links default to `FRONTEND_URL` and expire after 366 days. Run Celery
worker and Beat so due jobs and the nurture reconciler continue to execute.
