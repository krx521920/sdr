# AI outbound copy

The SDR outbound workspace can generate an auditable A/B email sequence from a
campaign ICP and an operator-provided product brief. Generation is asynchronous
and never activates or launches a sequence automatically.

## Safety boundary

- The model receives campaign context plus an anonymous aggregation of up to 10
  prospects' job titles, companies, industries, and countries. Names, addresses,
  contact methods, and raw prospect notes are never included.
- The same unified AI preflight used by lead qualification applies recursive
  field allow-lists, PII redaction, sensitive-content blocking, and input limits.
- Prompts prohibit invented customers, metrics, integrations, guarantees, and
  other unsupported proof. Operators must supply approved proof points.
- Provider responses must match the exact versioned JSON shape and use only the
  supported template variables.
- A generated draft remains separate from the nurture sequence until an operator
  reviews it and selects **Apply to inactive sequence**.
- Applying is allowed only while the campaign is `draft` or `paused`. It creates
  or updates an inactive outbound sequence and does not send any email.
- An existing sequence is reused only when it is inactive, has no enrollments,
  and is not shared by another campaign. Otherwise a new inactive sequence is
  created.

## Operator flow

1. Select a campaign in **Settings → SDR outbound**.
2. Enter the offering summary, value proposition, approved proof points, CTA,
   language, tone, and number of steps.
3. Generate the draft and wait for the durable background job to finish.
4. Review and edit every A/B subject, opening, body, CTA, delay, and rationale.
5. Save the human review, then apply it to an inactive sequence.
6. Complete the sender and launch configuration through the existing campaign
   workflow when the sequence is ready.

## API

- `GET|POST /api/sdr/outbound/campaigns/<campaign_id>/copy-drafts/`
- `GET|PATCH /api/sdr/outbound/copy-drafts/<draft_id>/`
- `POST /api/sdr/outbound/copy-drafts/<draft_id>/action/` with
  `{ "action": "apply" }`

The creation response includes the durable automation job ID. Drafts retain the
provider/model, prompt version, response ID, token counts, provider attempts,
reviewer, timestamps, and any terminal error for audit and troubleshooting.

## Provider configuration

Copy generation uses the same `UnifiedAIGateway`, organization policy, audit
ledger, routing, and encrypted credential resolution as qualification. OpenAI
and Doubao use Responses structured output;
DeepSeek uses Chat Completions JSON output followed by the same strict local
validation. Primary and fallback routes follow the configured provider registry.

The Celery worker must load the `sdr.generate_outbound_copy` durable-job handler.
No live provider request is required for infrastructure smoke tests: leaving SDR
Intelligence disabled must produce an `ai_gateway_not_enabled` dead-letter job
without contacting a model provider.
