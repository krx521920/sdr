# Unified AI safety gateway

Every external AI request in SDR crosses the same `UnifiedAIGateway` before a
provider adapter can be created. The gateway owns purpose and recursive field
allow-lists, PII handling, sensitive-content blocking, input limits, provider
selection, protocol translation, encrypted tenant credentials, failover, and a
metadata-only audit ledger. The SDR pipeline still owns each business decision.

## Route order

```text
tenant qualification policy
          |
          v
primary provider + model
          |
          +-- success ------------> validated qualification
          |
          +-- failure
                 |
                 v
        fallback provider + model
                 |
                 +-- success -----> validated qualification
                 |
                 +-- failure -----> deterministic rules-v1
```

Supported adapters:

| Provider | Protocol | Default deployment model |
| --- | --- | --- |
| OpenAI | Responses API with strict JSON Schema | `gpt-5.6-luna` |
| Doubao / Volcengine Ark | Responses API with structured output | `doubao-seed-2-0-lite-260215` |
| DeepSeek | Chat Completions with JSON Output | `deepseek-v4-flash` |

Every response is parsed and validated locally. Scores must be integers from
0-100, bands must match fixed thresholds, and all required fields must exist.
Provider compatibility never bypasses this validation.

## Fail-closed preflight

The preflight sequence is fixed:

1. Re-read the tenant AI switch, allowed purpose, and provider policy.
2. Reject every unregistered purpose or unknown nested input field.
3. Detect PII locally. The default is irreversible placeholders; `block`
   rejects all detected PII and `allow` requires an explicit tenant setting.
4. Block credentials, private keys, recovery keys, identity numbers, and payment
   cards regardless of the ordinary PII setting.
5. Enforce serialized character and conservative input-token limits.
6. Create a `pending` audit row. If this write fails, no HTTP call is made.
7. Invoke the primary provider; only provider failures may use the fallback,
   which receives the same sanitized context.

The audit table never stores the prompt, input text, output text, detected PII
values, or a provider response body. It stores organization, purpose, prompt and
configuration versions, provider/model route, field names, hashes, PII category
counts, redaction count, tokens, estimated cost, latency, and safe failure code.
It is protected by PostgreSQL RLS. Administrators can inspect it at:

```text
GET /api/sdr/intelligence/ai-audits/
```

Each row receives a tenant-configured expiry time (90 days by default). Celery
beat runs `sdr.purge_expired_ai_call_audits` daily and removes expired terminal
records without touching in-flight attempts.

## Real-provider canary

Use a dedicated test organization whose PII policy is `block`. The command is
network-free unless the explicit confirmation flag is present, never prints a
prompt, completion, credential, or provider response identifier, and sends one
synthetic qualification request with no fallback:

```console
python manage.py verify_ai_gateway_canary \
  --org-id <test-organization-uuid> \
  --provider openai

python manage.py verify_ai_gateway_canary \
  --org-id <test-organization-uuid> \
  --provider openai \
  --confirm-real-provider-call
```

Run the dry check first. Missing credentials, disabled tenant policy, non-blocking
PII policy, disallowed provider/purpose/model, or a non-clean synthetic input all
stop before provider-client construction. The confirmed command emits only safe
status, routing, token, latency, cost, and audit request identifiers.

## Tenant credentials

The deployment may provide a platform key for each provider. When
`AI_GATEWAY_ALLOW_TENANT_KEYS=True`, an organization administrator can instead
store a tenant-owned key. Tenant keys override the matching platform key and
are encrypted with `INTEGRATION_ENCRYPTION_KEY`; the API returns only the last
eight characters. Keys, ciphertext, and provider response bodies are never
included in inspection records.

Production requires an explicit valid Fernet `INTEGRATION_ENCRYPTION_KEY` and
refuses to start without it. New ciphertext is version-prefixed while legacy
unprefixed Fernet values remain decryptable. Rotate this key only through an
explicit re-encryption procedure; changing it directly makes stored tenant
credentials unreadable.

Provider base URLs and model allow-lists are deployment-owned. A tenant cannot
submit a custom URL, which prevents credential exfiltration through an
attacker-controlled OpenAI-compatible endpoint.

## Audit semantics

`provider_attempts` records the provider, model, completion status, safe error
code, retryability, and credential source for each attempted route. It contains
no prompt, response body, or secret. `fallback_kind` is:

- empty when the primary model succeeds;
- `model` when the configured fallback model succeeds;
- `rules` when deterministic scoring is used.

The selected provider/model, response identifier, token usage, prompt version,
configuration fingerprint, and validation outcome remain on the business
ledger. The unified `sdr_ai_call_audit` table records both successful and failed
provider attempts plus preflight blocks across qualification and copy generation.

## Deployment settings

```dotenv
OPENAI_API_KEY=""
OPENAI_API_BASE_URL="https://api.openai.com/v1"
OPENAI_ALLOWED_MODELS="gpt-5.6-luna,gpt-5.6-terra,gpt-5.6-sol"

DOUBAO_API_KEY=""
DOUBAO_API_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_ALLOWED_MODELS="doubao-seed-2-0-lite-260215"

DEEPSEEK_API_KEY=""
DEEPSEEK_API_BASE_URL="https://api.deepseek.com"
DEEPSEEK_ALLOWED_MODELS="deepseek-v4-flash,deepseek-v4-pro"

AI_GATEWAY_ALLOW_TENANT_KEYS="True"
AI_GATEWAY_ALLOWED_PROVIDERS="openai,doubao,deepseek"
AI_GATEWAY_ALLOWED_REASONING_EFFORTS="none,low,medium"
# Optional JSON keyed by "provider:model". Values are micro-USD per million tokens.
AI_GATEWAY_MODEL_PRICING="{}"
```

References:

- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [Volcengine Ark Responses API quick start](https://www.volcengine.com/docs/82379/1795150)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
