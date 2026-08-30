# Person → Evidence → Opportunity → Match

This bounded context extends SDR from finding customers to finding the right
person for any well-defined place: a customer account, job, project, expert
request, referral, contractor engagement, or partnership.

## Domain boundary

- `Person` is the tenant's canonical human record. Channel addresses live in
  `PersonIdentity`, so email, phone, LinkedIn, WhatsApp, WeChat, and external
  identifiers can converge without making any provider the source of truth.
- `Evidence` is source-attributed and time-aware. It stores a concise summary,
  structured facts, confidence, source reference, observation time, expiry, and
  a content hash. Raw chat history is not required by this model.
- `MatchOpportunity` is a matching demand and deliberately does not replace the
  existing CRM sales `Opportunity`. Its type can be customer, employment,
  contractor, project, expert, referral, or partnership.
- `Match` is the versioned assessment identity. Its independent
  `projection_state` distinguishes the one live recommendation projection from
  a retired historical projection without changing the operator's workflow
  decision status. It contains eligibility, fit, trust, relationship,
  availability, confidence, reasons, gaps, and rank.
- `MatchEvidence` cites evidence records contributing to the current projection.
- `MatchRun` records each durable recompute request and its fixed candidate
  snapshot. `MatchRevision` stores immutable evaluation, rerank, or retirement
  snapshots, while `MatchDecisionEvent` stores immutable reviewer decisions.
- `PersonImportBatch` and `PersonImportRecord` form the durable ingestion
  ledger. `PersonIdentityObservation` preserves where an identity was observed,
  `PersonImportConflict` and its append-only decisions hold ambiguous rows for
  review, and `PersonImportImpact` records exactly which people need matching
  recalculation.

Every table owns an `org_id`, is filtered in the API, and has PostgreSQL RLS
enabled and forced. This gives both application-layer and database-layer tenant
isolation. Database triggers also reject updates and deletes of match revisions
and decision events, require every current projection to reference an active,
governance-active Person in the same organization, and reject a raw Person
update that would strand a current projection. Audit history is therefore
append-only and current-projection eligibility is enforced below the
application layer.

## Criteria contract

`required_criteria`, `preferred_criteria`, and `exclusion_criteria` accept these
keys:

```json
{
  "skills": ["python", "django"],
  "titles": ["growth engineer"],
  "locations": ["shanghai"],
  "availability": ["available", "open_to_offers"]
}
```

`scoring_weights` uses the same keys. The default weights are skills 45, titles
20, locations 15, and availability 20.

The `rules-v2` engine is deterministic. It only uses fields and active evidence
stored for the person, never invents missing facts. Missing required criteria
cap the overall score at 49; matching any exclusion sets eligibility and overall
score to zero. Recomputing a match updates machine scores but preserves the
human review status.

AI providers may later turn unstructured source material into proposed
`Evidence`, but the matching result remains linked to stored evidence and can be
audited independently of the model response.

## API

All endpoints require authentication and organization context. Matching access
is fail-closed and separated into `read`, `manage`, `recompute`, and `decide`
capabilities; organization administrators receive the highest capability.

- `GET /api/matching/capabilities/`
- `GET/POST /api/matching/people/`
- `POST /api/matching/people/onboard/`
- `GET/PATCH /api/matching/people/{person_id}/`
- `GET/POST /api/matching/identities/`
- `GET/POST /api/matching/evidence/`
- `GET/POST /api/matching/opportunities/`
- `GET/PATCH /api/matching/opportunities/{opportunity_id}/`
- `GET/POST /api/matching/opportunities/{opportunity_id}/matches/` (compatibility
  recompute entry point)
- `POST /api/matching/opportunities/{opportunity_id}/recompute/`
- `GET /api/matching/opportunities/{opportunity_id}/match-runs/`
- `GET /api/matching/match-runs/{run_id}/`
- `GET/PATCH /api/matching/matches/{match_id}/`
- `GET /api/matching/matches/{match_id}/revisions/`
- `GET /api/matching/matches/{match_id}/decisions/`
- `POST /api/matching/person-imports/preview/`
- `GET /api/matching/person-imports/`
- `GET /api/matching/person-imports/{import_id}/`
- `GET /api/matching/person-imports/{import_id}/records/`
- `POST /api/matching/person-imports/{import_id}/commit/`
- `POST /api/matching/person-import-records/{record_id}/resolve/`

The onboarding endpoint creates one `Person`, zero to twenty normalized
identities, and one to fifty evidence records in a single transaction. It
requires `manage` access and a UUID `Idempotency-Key`. Reusing the same key with
the same normalized request safely returns the original graph; changing the
request or colliding with an identity returns `409` without leaving a partial
person. The original child-ID receipt is stored with the Person, so a replay
does not absorb Identity or Evidence records added later. Manual onboarding and
the public direct-write endpoints only accept `source=manual`, bounded matching
facts, and HTTP(S) references without credentials or sensitive query/fragment
parameters. Direct Evidence responses omit raw facts and provider reference
fields. Provider adapters write authenticated provenance through internal
integration paths instead of impersonating a source through an operator API.
PostgreSQL constraints and triggers enforce the organization boundary even for
raw child-table writes, and the trigger migration refuses to install over
historical cross-organization child rows.

Evidence is append-only through the first API slice: there is no update or
delete endpoint. During a full opportunity recompute, all active people in the
tenant are snapshotted. Recompute requires a UUID
`Idempotency-Key`, persists an `AutomationJob` and `MatchRun`, and returns `202`
before a Celery worker evaluates up to 500 people. Reusing a key with a different
request is rejected. If a queued candidate snapshot changes before execution,
the job fails closed instead of silently evaluating a different population.
Evaluation and rank changes are preserved as immutable revisions tied to the
run.

Embedded match responses expose a minimal person summary and evidence citation
metadata, never raw evidence facts, source URLs, or provider record identifiers.

### Current projection integrity

`Match.status` remains the human workflow decision (`proposed`, `reviewing`,
`shortlisted`, `accepted`, or `rejected`). Projection lifecycle is separate:
only `projection_state=current` rows whose Person has both `status=active` and
`governance_status=active` participate in ranking, default match APIs, decision
mutations, feedback mutations, or the feedback queue.

Changing a Person from matchable to inactive, deletion-requested, anonymized,
or otherwise unavailable atomically retires all of that Person's current
projections. Retirement sets `retired_at` and a bounded reason, clears the old
rank, creates a completed system `MatchRun` with an immutable retirement
revision, and continuously reranks the remaining eligible candidates. Existing
decision state, revisions, decision events, and feedback events are retained.
Their history endpoints remain readable even though default current-match
endpoints return `404`.

Cancelling a deletion request does not silently republish an old recommendation.
The retired projection becomes current only after a new explicit recompute.
Every projection mutation first locks its organization row as a tenant-scoped
mutex, then takes lower-level Person, Opportunity, and Match locks. This keeps
same-tenant retirement and recompute writes in one order, prevents deferred
foreign-key checks from completing a deadlock cycle, and still allows different
organizations to proceed in parallel. PostgreSQL triggers protect raw SQL and
bulk updates; every read, mutation, and global-rank query repeats the eligibility
filter so SQLite tests and maintenance scripts do not rely on that single
database guard.

## Unified import pipeline

CSV is the first transport over a channel-neutral ingestion service. A preview
accepts at most 5 MiB and 500 non-empty UTF-8 rows, validates an explicit
allow-listed field mapping, and persists only the bounded normalized rows. The
original file is not retained. Each row must include a display name and at
least one stable identity; names alone are never used to merge people.

Previewing creates a tenant-owned batch that can be reopened after navigation
or refresh. Committing the batch persists an `AutomationJob` whose payload
contains only organization, batch, schema-version, and request-hash values. It
does not place CSV content, identity values, or provider credentials in the
broker. A worker processes every row in its own transaction:

1. No matching identity creates a new canonical person.
2. Identities that all resolve to one person perform a non-destructive merge:
   empty scalar fields may be filled and skills or roles are unioned, but
   existing non-empty profile values are not overwritten.
3. Identities that resolve to different people create a review conflict and do
   not write Person, Evidence, or Impact state.
4. A previously applied source record with the same normalized content is a
   replay; changed content for the same source record is reviewed rather than
   silently overwriting evidence.

Database identity uniqueness is the final concurrency lock. When two workers
race to introduce the same normalized identity, the losing transaction resolves
the winner and either performs the same safe merge or records a conflict. Import
records preserve source namespace, original source record ID, observation time,
confidence, and a content fingerprint. API representations mask identities and
omit normalized payloads, source URIs, broker payloads, and internal exception
messages.

Executable normalized row payloads now have an explicit lifecycle. They remain
available only while a preview can still be committed or a conflict needs a
decision, and are cleared when a row reaches a terminal state. Person
anonymization also clears linked import display names, masked identities, and
validation details. An organization-scoped bounded expiry service scrubs one
bounded page of abandoned previews at minute 45 of every hour. The retention
window defaults to seven days and is bounded to 1..365 days through
`MATCHING_IMPORT_PREVIEW_RETENTION_DAYS`; changing it is an explicit deployment
governance decision rather than an unbounded staging-data exception.

Only a created person or a merge that changes matching-relevant state produces
`PersonImportImpact`. Once an import commits, the affected person IDs are sent
to deterministic, idempotent recompute runs for open opportunities. Invalid,
replayed, skipped, and unresolved rows never trigger evaluation. Re-ranking may
change other candidates' positions, but their evidence and scores are not
re-evaluated.

The first operator UI is available at `/matching`. It keeps opportunity and
filter selection in the URL. A user with `manage` access can create a structured
matching opportunity or use the four-step Person → Identity → Evidence → Review
wizard directly from the workbench. The wizard masks the identity during review,
preserves a failed draft for correction, and offers a focused recompute against
the selected opportunity after a successful save. Users with `recompute` access
can confirm and queue a run; users with `decide` access can move the current
match through review statuses. The forms accept bounded structured criteria and
do not expose arbitrary JSON input.

## Channel integration path

Apollo, LinkedIn, WhatsApp, WeChat, Feishu, email, CRM, manual entry, and AI are
evidence producers. They must submit normalized source records through the same
ingestion service used by CSV instead of implementing provider-specific person
creation. Provider adapters should:

1. Resolve or create a `PersonIdentity` using a normalized provider identifier.
2. Resolve the canonical `Person` within the same tenant.
3. Store concise, structured `Evidence` with a source record ID for idempotency.
4. Apply provenance, consent, retention, and deletion rules before persisting.
5. Recompute only the affected person's open opportunities.

This separation keeps acquisition and conversation channels replaceable while
making people, evidence, opportunities, and match decisions a unified product
core.

### Real-channel execution safety

Provider credentials never authorize an external side effect by themselves.
Every production provider call must be wrapped by a durable execution request
that is bound to one organization, channel, action, HMAC-addressed test target,
canonical payload hash, unit cost, short-lived single-use approval, and UUID
idempotency key. Reservation locks both the organization-wide and channel-level
daily usage projections. Immediately before network I/O the worker rechecks the
environment, organization, channel, and test-target kill switches.

The request state machine is `RESERVED -> SENDING -> ACCEPTED -> DELIVERED`,
with `FAILED` for a known rejection before provider acceptance and `UNKNOWN`
for a call whose provider outcome or local projection cannot be proven. Units
are released only for a known pre-acceptance failure. `UNKNOWN` remains charged
and cannot be automatically replayed; an operator must reconcile it using
sanitized provider evidence. WeChat and WeCom are hard-disabled until official
providers and the privacy review exist.

The transition period is intentionally fail-closed. Existing Apollo, Feishu,
Email, WhatsApp, and LinkedIn clients reject legacy production jobs that do not
carry a valid `SENDING` execution-request ID. Each channel is enabled only after
its enqueue contract creates and passes that ID. Apollo additionally treats a
search and every person enrichment as separate credit-bearing calls; one
execution request cannot be reused across those operations.

Apollo's implemented projection stores search candidates without provider PII,
then imports only separately approved enrichment results through the shared
Person import preview/commit ledger. Two workers cannot claim the same request:
the durable `RESERVED -> SENDING` transition is serialized in PostgreSQL. A
five-minute organization-scoped reconciler refunds stale pre-I/O reservations,
marks stale in-flight calls `UNKNOWN`, aligns the candidate with an operator's
manual resolution, and projects import-batch terminal states back to the
candidate. This recovery path never performs provider I/O.

Feishu Base now implements separate approved actions for schema validation,
one-record upsert, deletion, and bounded Person-import reads. All four bind to the same opaque internal
connection target, enforce bounded reads and at most one mutation, use a
single-attempt durable job, and retain an uncertain result as charged `UNKNOWN`.
Remote record IDs are encrypted at rest and never appear in browser projections,
jobs, or provider-error details. Its legacy automatic-sync and custom-bot paths
remain disabled. The inbound action stores its one-off field mapping encrypted,
keeps raw Base record IDs in memory only, and converts them to organization- and
destination-bound HMAC identifiers. It creates only a shared import preview;
Person and Evidence writes still require the existing explicit batch commit.
The request stays `SENDING` through the provider read and local preview, then
atomically settles quota, `PREVIEWED`, and `DELIVERED`. A worker crash rolls the
transaction back to `SENDING` and the stale reconciler moves both ledgers to
charged `UNKNOWN` without another provider read.

## Evidence governance and contact intent

Every Evidence row has a one-to-one `EvidenceProvenance` projection and an
append-only governance event history. Public APIs store only a source-content
SHA-256 digest, concise summary, allow-listed matching facts, and bounded legal
metadata; raw messages, transcripts, chats, provider payloads, credentialed
URLs, and identity values are rejected or masked. Existing non-AI evidence is
backfilled as confirmed while AI-derived evidence remains pending until a human
reviewer confirms or rejects it.

The matching engine fails closed: it evaluates only evidence that is confirmed,
active, observed, not past its evidence validity, and not past its retention
date. Evidence review, provenance updates, contact-intent decisions, exports,
deletion requests, and anonymization all use tenant-scoped revision checks and
UUID idempotency keys. A confirmed objection creates SDR do-not-contact entries
for every matching identity on the affected channel; the intent itself remains
the fail-closed control when no usable identity exists.

Organization administrators can run safe exports, manage deletion, and execute
retention scans. Anonymization preserves graph IDs and immutable history while
irreversibly replacing addressable identities and raw matching facts. Daily
Celery beat scans run per organization under an explicit database RLS context;
one tenant failure is logged and isolated from the remaining tenants. Expired
or restricted evidence triggers deterministic person-targeted recompute runs for
open opportunities.

## Match feedback and scoring governance

The feedback loop keeps three different facts separate:

1. A `MatchDecisionEvent` records the operator workflow decision, such as
   shortlisting, accepting, or rejecting a recommendation.
2. A recommendation feedback event records whether the recommendation was
   accurate, partly accurate, inaccurate, or still uncertain.
3. A lifecycle outcome records what later happened, such as contact, interview,
   deal, hire, referral, or collaboration. An accepted recommendation is never
   treated as a successful lifecycle outcome by itself.

Feedback and outcomes are append-only tenant records. Corrections point to the
event they supersede instead of rewriting history. Every mutation is protected
by a UUID idempotency key, the expected feedback revision, and the ranking
revision on which the reviewer acted. Evidence assessments may only cite
evidence belonging to the matched person and captured by that ranking revision.
Free-form notes are never included in aggregate calibration input.

Feedback analytics report observed associations, not causal effects. Dimension
and outcome slices below the configured minimum sample size are suppressed, and
responses do not expose people, identities, raw evidence facts, provider
payloads, prompts, or reviewer notes.

Scoring policy is versioned per organization and opportunity type. A run pins
the exact policy version and checksum used for evaluation, and its immutable
match revision stores the resolved dimension and component weights. Historical
runs therefore remain reproducible after a new policy is published.

An AI or analysis process may only create a pending weight suggestion from
privacy-safe aggregate data. Human acceptance copies the suggestion into a
draft policy version; it does not change live scoring. An organization
administrator must explicitly publish that draft in a separate, audited action
before a later matching run can use it. The system never allows a model response
to update active weights directly.

## Product focus and channel rollout

The technical core can match people to customer, employment, expert,
partnership, and other opportunities, but the near-term commercial entry point
remains SDR customer acquisition. Daily operator language should therefore use
prospect, account, audience, campaign, and follow-up; `Person`, `Evidence`,
`MatchOpportunity`, and `Match` remain the shared domain underneath those SDR
workflows. New non-customer use cases must reuse the same core instead of
creating separate person graphs or evidence stores.

Channel ingestion is delivered in this order:

1. CSV and the internal CRM;
2. Apollo;
3. Feishu Base;
4. email;
5. WhatsApp;
6. LinkedIn;
7. WeChat or WeCom.

Every source must enter the same import ledger and produce canonical people,
normalized identities, source-attributed evidence, conflicts, impacts, and
targeted recomputes. A Campaign prospect is a person's membership in one
campaign, not a second person master record.

External credentials do not make a channel production-ready. Provider calls
remain blocked until a dedicated integration organization, exact test objects,
audited approval, consent and DNC checks, quota or credit reservation, and an
unknown-delivery reconciliation path are all present. WeChat and WeCom remain
explicitly unavailable until an official provider implementation and the
stronger privacy and account-safety review are complete.

The product must first prove three outcomes:

- records are not lost or duplicated and their source remains traceable;
- an operator can find a suitable person faster than with manual work;
- every recommendation is explainable and can be evaluated against an observed
  business outcome.
