# Durable automation jobs

Lead-channel events must survive broker outages, worker crashes, and provider
redelivery. The automation job ledger provides that reliability without moving
provider or SDR business rules into the `automation` bounded context.

## Lifecycle

```text
provider event
    |
    v
persist pending job -- duplicate idempotency key --> existing job
    |
    v
publish to broker --> queued --> running --> succeeded
                                  |
                                  +--> retry scheduled --+
                                  |                      |
                                  +--> dead letter <-----+
                                           |
                                           +--> administrator replay
```

The unique key is `(organization, job name, idempotency key)`. Facebook Lead
Ads uses `leadgen:<leadgen_id>`, so Meta may deliver the same event repeatedly
without creating another CRM lead or another job record.

## Failure guarantees

- A job is committed to PostgreSQL before Celery publication is attempted.
- If publication fails, the job returns to `pending`; the webhook can return
  `503`, while the periodic dispatcher independently recovers the same row.
- Claims use a database row lock. Duplicate Celery messages are safe because
  only one worker can transition a job to `running`.
- Retryable failures use exponential backoff. Every claim creates an immutable
  attempt record with its outcome and safe error details.
- Permanent failures, or jobs that exhaust `max_attempts`, enter
  `dead_letter`. They remain visible until an organization administrator
  replays them.
- Queued or running jobs whose lease expires are recovered by the scheduler.

Both `automation_job` and `automation_job_attempt` are organization-owned and
protected by PostgreSQL RLS. The global scheduler enumerates organizations and
sets one explicit RLS context at a time; it never scans tenant payloads without
a tenant context.

## Operations

Run both a Celery worker and Celery Beat. Beat invokes
`automation.dispatch_due_jobs` every minute. Organization administrators can
monitor work at `/settings/automation` or through:

```text
GET  /api/automation/jobs/
GET  /api/automation/jobs/<job-id>/
POST /api/automation/jobs/<job-id>/retry/
```

The retry endpoint accepts only a `dead_letter` job from the caller's current
organization. A replay retains prior attempt history, extends the attempt
budget, and immediately republishes the job when the broker is available.

## Settings

```dotenv
AUTOMATION_RETRY_BASE_SECONDS="5"
AUTOMATION_RETRY_MAX_SECONDS="900"
AUTOMATION_JOB_LEASE_SECONDS="600"
AUTOMATION_MANUAL_RETRY_ATTEMPTS="3"
```

Job handlers are an application-controlled allow-list in Django settings. A
request payload can choose only a registered job name, never an arbitrary
Python import path.
