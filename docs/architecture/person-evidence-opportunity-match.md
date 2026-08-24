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
- `Match` is a versioned assessment. It contains eligibility, fit, trust,
  relationship, availability, confidence, reasons, gaps, and a stable rank.
- `MatchEvidence` cites the exact evidence records contributing to a match.

Every table owns an `org_id`, is filtered in the API, and has PostgreSQL RLS
enabled and forced. This gives both application-layer and database-layer tenant
isolation.

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

The `rules-v1` engine is deterministic. It only uses fields and active evidence
stored for the person, never invents missing facts. Missing required criteria
cap the overall score at 49; matching any exclusion sets eligibility and overall
score to zero. Recomputing a match updates machine scores but preserves the
human review status.

AI providers may later turn unstructured source material into proposed
`Evidence`, but the matching result remains linked to stored evidence and can be
audited independently of the model response.

## API

All endpoints require authentication, organization context, and sales access.

- `GET/POST /api/matching/people/`
- `GET/PATCH /api/matching/people/{person_id}/`
- `GET/POST /api/matching/identities/`
- `GET/POST /api/matching/evidence/`
- `GET/POST /api/matching/opportunities/`
- `GET/PATCH /api/matching/opportunities/{opportunity_id}/`
- `GET/POST /api/matching/opportunities/{opportunity_id}/matches/`
- `GET/PATCH /api/matching/matches/{match_id}/`

Evidence is append-only through the first API slice: there is no update or
delete endpoint. Recompute accepts an optional `person_ids` list; without it,
all active people in the tenant are evaluated.

## Channel integration path

Apollo, LinkedIn, WhatsApp, WeChat, Feishu, email, CRM, manual entry, and AI are
evidence producers. Provider adapters should:

1. Resolve or create a `PersonIdentity` using a normalized provider identifier.
2. Resolve the canonical `Person` within the same tenant.
3. Store concise, structured `Evidence` with a source record ID for idempotency.
4. Apply provenance, consent, retention, and deletion rules before persisting.
5. Recompute only the affected person's open opportunities.

This separation keeps acquisition and conversation channels replaceable while
making people, evidence, opportunities, and match decisions a unified product
core.
