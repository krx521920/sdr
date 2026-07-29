# AI lead inspector

The lead inspector enriches a normalized inbound lead with small-scale public
company website research and qualifies it against an organization-specific
ideal customer profile (ICP). It implements the existing SDR enrichment and
scoring ports, so provider adapters remain independent of research and model
vendors.

## Runtime flow

```text
normalized lead
    |
    +--> email/domain enrichment
    |
    +--> public website research (optional, max 1-3 pages)
    |
    +--> deterministic baseline score
    |
    +--> multi-model gateway qualification (optional)
    |         |
    |         +-- failure / invalid output --> fallback model (optional)
    |                                             |
    |                                             +-- failure --> baseline score
    |
    +--> country/source/score routing
    |
    +--> CRM handoff + auditable inspection evidence
```

Facebook execution remains inside its durable automation job. The authenticated
website intake endpoint performs inspection before returning so its caller gets
the completed CRM handoff; administrators can cap page count and per-page
website timeout for latency control.

## Model gateway contract

The inspector calls the tenant's primary model route and optionally a fallback
route through the [SDR model gateway](model-gateway.md). OpenAI and Doubao use
Responses adapters; DeepSeek uses a Chat Completions adapter. Provider output is
normalized into one qualification contract and validated locally before it can
affect routing or CRM data.

The model receives no contact name, email address, or phone number. It receives
the job title, whether contact methods exist, company data, the tenant ICP, the
baseline reasons, and bounded website text. The prompt explicitly treats all
website content as untrusted data. Returned scores must be 0-100 and agree with
the platform's fixed band thresholds or the response is rejected.

References:

- [OpenAI model selection](https://developers.openai.com/api/docs/models)
- [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Responses structured output](https://platform.openai.com/docs/api-reference/responses)

## Website safety and evidence

Research accepts only HTTP(S) URLs on ports 80/443. It resolves and rejects
loopback, private, link-local, reserved, multicast, and other non-public
addresses before every request. Redirects and selected About/Company links must
remain on the same base host. Fetches disable automatic redirects, stream at
most 256 KB per page, accept only HTML, and retain at most 18,000 characters for
model input.

Raw page text is not persisted. The inspection stores source URLs, a SHA-256
content digest, a bounded summary, structured facts, qualification reasons,
provider/model identifiers, a tenant-configuration fingerprint, token usage,
and a safe error code/message.

## Failure semantics

- Missing API key, rate limit, provider failure, refusal, invalid JSON, or a
  score/band mismatch tries the configured model fallback and then deterministic
  `rules-v1` scoring.
- Website DNS, network, redirect, size, and content errors skip that research
  while still allowing qualification from submitted lead data.
- A fallback produces a `partial` inspection and never prevents CRM creation or
  routing.
- An unexpected pipeline or database failure marks the inspection `failed` and
  follows the existing durable intake retry behavior.

## Tenant isolation and administration

`sdr_intelligence_settings`, `sdr_model_credential`, and
`sdr_lead_inspection` carry `org_id` and are protected by PostgreSQL row-level
security. The REST endpoints additionally require an organization administrator
and filter every lookup by the current organization.

Administration UI: `/settings/sdr-intelligence`

```text
GET/PUT/PATCH /api/sdr/intelligence/settings/
GET           /api/sdr/intelligence/inspections/
GET           /api/sdr/intelligence/inspections/<inspection-id>/
```

## Deployment

Apply `sdr.0004_multi_model_gateway` and configure:

```dotenv
# See model-gateway.md for OpenAI, Doubao, DeepSeek, and BYOK settings.
```

The inspector is disabled for every organization by default. An administrator
must define the ICP and enable it explicitly.
