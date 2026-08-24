# SDR model gateway

The model gateway keeps lead qualification independent from a single model
vendor. It owns provider selection, protocol translation, encrypted tenant
credentials, deployment allow-lists, failover, and a normalized qualification
result. The SDR pipeline still owns the business decision and always retains
deterministic scoring as its final fallback.

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
configuration fingerprint, and validation outcome remain on the lead
inspection ledger.

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
AI_GATEWAY_ALLOWED_REASONING_EFFORTS="none,low,medium"
```

References:

- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [Volcengine Ark Responses API quick start](https://www.volcengine.com/docs/82379/1795150)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
