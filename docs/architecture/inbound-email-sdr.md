# Inbound email for SDR

The existing signed AWS SES/SNS receiver can route each tenant mailbox to
support tickets or the SDR pipeline. `case` remains the default route, so
existing mailboxes retain their prior behavior after migration.

## Flow

```text
AWS SES receipt rule
        |
        v
signed SNS webhook
        |
        +-- verify signature / reject spam and bounces
        +-- de-duplicate RFC Message-ID
        +-- persist EmailMessage audit row
        |
        v
mailbox.route_target
        |
        +-- case -> existing ticket threading and creation
        |
        +-- sdr  -> persist sdr.process_inbound_email job
                         |
                         +-- sender matches nurture enrollment
                         |      -> classify reply
                         |      -> stop future messages
                         |      -> notify assigned sales owner
                         |
                         +-- no enrollment match
                                -> normalize email lead
                                -> research / qualify / route
                                -> CRM handoff
```

The public webhook remains anchored on SNS signature verification and the
unguessable mailbox UUID. It establishes the organization RLS context before
reading or writing tenant data.

## Reply handling

Replies are matched by tenant and CRM lead email against active, paused, or
recently completed nurture enrollments. The latest sent nurture delivery stores
the inbound RFC Message-ID, reply timestamp, and sentiment.

Sentiment is deterministic in this phase. Opt-out and negative phrases are
evaluated before positive phrases so text such as "not interested" cannot be
misclassified as positive. Explicit unsubscribe language cancels the enrollment
and creates a tenant-level suppression that blocks future automatic nurture;
other replies stop it with `replied` status. The original message body remains in
the RLS-protected `email_message` audit table and is not copied to metrics.

## New inbound leads

When no nurture enrollment matches, the message is normalized as an SDR `email`
source. Display name becomes the contact name. Non-consumer sender domains may
seed company name and website research; common personal email domains do not.
The existing deduplication, qualification, routing, CRM handoff, acknowledgement,
and nurture policies then apply unchanged.

## Operations

Administrators choose **Support tickets** or **SDR leads and nurture replies** at
`/settings/inbound-email`. Only SES full-content SNS delivery is currently
implemented by the receiver.

Apply migrations:

```text
cases.0024_sdr_inbound_email
sdr.0007_inbound_email_reply
```

Run a Celery worker and the normal durable-job dispatcher. A broker outage leaves
the persisted SDR email job pending for later recovery.
