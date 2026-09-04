"""Fail-closed data policy enforcement for every external AI invocation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from sdr.intelligence.contracts import ModelProviderError


class AISafetyError(ModelProviderError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message, code=code, retryable=False)


@dataclass(frozen=True, slots=True)
class AIUseCaseContract:
    purpose: str
    allowed_field_paths: frozenset[str]
    static_token_budget: int


@dataclass(frozen=True, slots=True)
class PreparedAIContext:
    purpose: str
    canonical_json: str
    input_sha256: str
    field_paths: tuple[str, ...]
    pii_findings: dict[str, int]
    redaction_count: int
    input_chars: int
    estimated_input_tokens: int
    _attestation: object = field(repr=False, compare=False)


LEAD_QUALIFICATION_CONTRACT = AIUseCaseContract(
    purpose="lead_qualification",
    static_token_budget=7000,
    allowed_field_paths=frozenset(
        {
            "source",
            "person.job_title",
            "person.has_business_email",
            "person.has_phone",
            "person.message",
            "company.name",
            "company.website",
            "company.industry",
            "company.country",
            "baseline.score",
            "baseline.band",
            "baseline.reasons[]",
            "tenant_icp.description",
            "tenant_icp.positive_signals",
            "tenant_icp.negative_signals",
            "website_research.source_urls[]",
            "website_research.content",
            "historical_sales_feedback.lookback_days",
            "historical_sales_feedback.sample_size",
            "historical_sales_feedback.overall_acceptance_rate",
            "historical_sales_feedback.acceptance_by_predicted_band[].band",
            "historical_sales_feedback.acceptance_by_predicted_band[].sample_size",
            "historical_sales_feedback.acceptance_by_predicted_band[].acceptance_rate",
            "historical_sales_feedback.top_rejection_reasons[].reason",
            "historical_sales_feedback.top_rejection_reasons[].label",
            "historical_sales_feedback.top_rejection_reasons[].count",
        }
    ),
)

OUTBOUND_COPY_CONTRACT = AIUseCaseContract(
    purpose="outbound_copy",
    static_token_budget=12000,
    allowed_field_paths=frozenset(
        {
            "request.language",
            "request.tone",
            "request.step_count",
            "request.offering_summary",
            "request.value_proposition",
            "request.proof_points",
            "request.cta_goal",
            "campaign.name",
            "campaign.description",
            "campaign.icp_description",
            "campaign.channels[]",
            "audience.job_titles[]",
            "audience.industries[]",
            "audience.countries[]",
            "audience.company_names[]",
            "audience.prospect_count",
            "allowed_template_variables[]",
        }
    ),
)

USE_CASE_CONTRACTS = {
    contract.purpose: contract
    for contract in (LEAD_QUALIFICATION_CONTRACT, OUTBOUND_COPY_CONTRACT)
}

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_LABELED_NAME_RE = re.compile(
    r"(?i)\b(?:full\s+name|contact\s+name|name)\s*[:=]\s*"
    r"[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3}"
)
_CHINESE_NAME_RE = re.compile(r"(?:姓名|联系人)\s*[：:]\s*[\u3400-\u9fff·]{2,20}")
_CONTEXTUAL_NAME_RE = re.compile(
    # Only the introducing phrase is case-insensitive. Making the entire
    # expression case-insensitive caused ordinary prose such as "I am
    # evaluating a workflow" to be classified as a person's name.
    r"(?i:\b(?:i\s+am|i['’]m|please\s+contact|contact|reach\s+out\s+to|"
    r"speak\s+with))\s+(?:(?i:mr\.?|mrs\.?|ms\.?|dr\.?)\s+)?"
    r"[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3}\b"
)
_COMMON_CHINESE_SURNAME = (
    r"(?:欧阳|太史|端木|上官|司马|东方|独孤|南宫|万俟|闻人|夏侯|诸葛|"
    r"尉迟|公羊|赫连|澹台|皇甫|宗政|濮阳|公冶|太叔|申屠|公孙|慕容|"
    r"仲孙|钟离|长孙|宇文|司徒|鲜于|司空|闾丘|子车|亓官|司寇|巫马|"
    r"公西|颛孙|壤驷|公良|漆雕|乐正|宰父|谷梁|拓跋|夹谷|轩辕|令狐|"
    r"段干|百里|呼延|东郭|南门|羊舌|微生|梁丘|左丘|东门|西门|南宫|"
    r"第五|赵|钱|孙|李|周|吴|郑|王|冯|陈|褚|卫|蒋|沈|韩|杨|朱|秦|"
    r"尤|许|何|吕|施|张|孔|曹|严|华|金|魏|陶|姜|戚|谢|邹|喻|柏|"
    r"水|窦|章|云|苏|潘|葛|奚|范|彭|郎|鲁|韦|昌|马|苗|凤|花|方|"
    r"俞|任|袁|柳|酆|鲍|史|唐|费|廉|岑|薛|雷|贺|倪|汤|滕|殷|罗|"
    r"毕|郝|邬|安|常|乐|于|时|傅|皮|卞|齐|康|伍|余|元|卜|顾|孟|"
    r"平|黄|和|穆|萧|尹|姚|邵|湛|汪|祁|毛|禹|狄|米|贝|明|臧|计|"
    r"伏|成|戴|谈|宋|茅|庞|熊|纪|舒|屈|项|祝|董|梁|杜|阮|蓝|闵|"
    r"席|季|麻|强|贾|路|娄|危|江|童|颜|郭|梅|盛|林|刁|钟|徐|邱|"
    r"骆|高|夏|蔡|田|樊|胡|凌|霍|虞|万|支|柯|管|卢|莫|经|房|裘|"
    r"缪|干|解|应|宗|丁|宣|贲|邓|郁|单|杭|洪|包|诸|左|石|崔|吉|"
    r"钮|龚|程|嵇|邢|滑|裴|陆|荣|翁|荀|羊|於|惠|甄|曲|家|封|芮|"
    r"羿|储|靳|汲|邴|糜|松|井|段|富|巫|乌|焦|巴|弓|牧|隗|山|谷|"
    r"车|侯|宓|蓬|全|郗|班|仰|秋|仲|伊|宫|宁|仇|栾|暴|甘|钭|厉|"
    r"戎|祖|武|符|刘|景|詹|束|龙|叶|幸|司|韶|郜|黎|蓟|薄|印|宿|"
    r"白|怀|蒲|邰|从|鄂|索|咸|籍|赖|卓|蔺|屠|蒙|池|乔|阴|郁|胥|"
    r"能|苍|双|闻|莘|党|翟|谭|贡|劳|逄|姬|申|扶|堵|冉|宰|郦|雍|"
    r"郤|璩|桑|桂|濮|牛|寿|通|边|扈|燕|冀|郏|浦|尚|农|温|别|庄|"
    r"晏|柴|瞿|阎|充|慕|连|茹|习|宦|艾|鱼|容|向|古|易|慎|戈|廖|"
    r"庾|终|暨|居|衡|步|都|耿|满|弘|匡|国|文|寇|广|禄|阙|东|欧|"
    r"殳|沃|利|蔚|越|夔|隆|师|巩|厍|聂|晁|勾|敖|融|冷|訾|辛|阚|"
    r"那|简|饶|空|曾|毋|沙|乜|养|鞠|须|丰|巢|关|蒯|相|查|后|荆|"
    r"红|游|竺|权|逯|盖|益|桓|公)"
)
_CHINESE_CONTEXT_NAME_RE = re.compile(
    rf"(?:我是|我叫|请联系|请找|联系人是)\s*{_COMMON_CHINESE_SURNAME}"
    r"[\u3400-\u9fff·]{1,2}(?![\u3400-\u9fff·])"
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|"
    r"authorization)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']{4,}"
)
_STANDALONE_SECRET_RE = re.compile(
    r"(?i)\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._~+/-]{8,})"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_CHINESE_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_RECOVERY_KEY_RE = re.compile(r"(?<!\d)(?:\d{6}-){7}\d{6}(?!\d)")
# Prepared contexts never leave this process, so a fresh process-local key can
# bind the sanitized payload to the metadata produced by preflight.  A plain
# sentinel only proves that an object was once prepared: ``dataclasses.replace``
# copies that sentinel while allowing any other field to be changed.
_PREPARED_CONTEXT_ATTESTATION_KEY = secrets.token_bytes(32)
_PREPARED_CONTEXT_ATTESTATION_DOMAIN = "sdr-prepared-ai-context-v1"


def prepare_ai_context(
    *,
    purpose: str,
    context: dict[str, Any],
    pii_handling: str,
    max_chars: int,
    max_tokens: int,
) -> PreparedAIContext:
    """Validate a registered schema and return only locally sanitized data."""

    contract = USE_CASE_CONTRACTS.get(purpose)
    if contract is None:
        raise AISafetyError(
            "The AI purpose is not registered.", code="ai_purpose_not_registered"
        )
    if not isinstance(context, dict):
        raise AISafetyError("AI input must be an object.", code="ai_input_invalid")
    if pii_handling not in {"redact", "block", "allow"}:
        raise AISafetyError(
            "The tenant PII policy is invalid.", code="ai_pii_policy_invalid"
        )

    canonical_before = _canonical_json(context)
    if len(canonical_before) > max_chars:
        raise AISafetyError(
            "AI input exceeds the tenant text limit.", code="ai_input_too_large"
        )

    leaves = tuple(sorted(_leaf_paths(context)))
    unknown = [
        path for path in leaves if not _path_allowed(path, contract.allowed_field_paths)
    ]
    if unknown:
        raise AISafetyError(
            f"AI input contains a field outside the allow-list: {unknown[0]}",
            code="ai_field_not_allowed",
        )

    findings: dict[str, int] = {}
    sanitized, redactions = _sanitize_value(
        context,
        pii_handling=pii_handling,
        findings=findings,
    )
    canonical = _canonical_json(sanitized)
    chars = len(canonical)
    # One Unicode code point per token is deliberately conservative for the
    # supported prose inputs and avoids a provider-specific tokenizer bypass.
    # Include a conservative, purpose-specific allowance for system
    # instructions and JSON schema so the limit covers the complete request,
    # not only business context.
    estimated_tokens = chars + contract.static_token_budget
    if chars > max_chars:
        raise AISafetyError(
            "Sanitized AI input exceeds the tenant text limit.",
            code="ai_input_too_large",
        )
    if estimated_tokens > max_tokens:
        raise AISafetyError(
            "AI input exceeds the tenant token limit.", code="ai_token_limit_exceeded"
        )
    sorted_findings = dict(sorted(findings.items()))
    attestation = _prepared_context_attestation(
        purpose=purpose,
        canonical_json=canonical,
        input_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        field_paths=leaves,
        pii_findings=sorted_findings,
        redaction_count=redactions,
        input_chars=chars,
        estimated_input_tokens=estimated_tokens,
    )
    return PreparedAIContext(
        purpose=purpose,
        canonical_json=canonical,
        input_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        field_paths=leaves,
        pii_findings=sorted_findings,
        redaction_count=redactions,
        input_chars=chars,
        estimated_input_tokens=estimated_tokens,
        _attestation=attestation,
    )


def prepared_context_json(value: PreparedAIContext, *, expected_purpose: str) -> str:
    """Return attested sanitized JSON; raw dictionaries are never accepted."""

    if not isinstance(value, PreparedAIContext):
        raise AISafetyError(
            "A gateway-prepared AI context is required.",
            code="ai_context_not_prepared",
        )
    try:
        expected = _prepared_context_attestation(
            purpose=value.purpose,
            canonical_json=value.canonical_json,
            input_sha256=value.input_sha256,
            field_paths=value.field_paths,
            pii_findings=value.pii_findings,
            redaction_count=value.redaction_count,
            input_chars=value.input_chars,
            estimated_input_tokens=value.estimated_input_tokens,
        )
        verified = (
            value.purpose == expected_purpose
            and isinstance(value._attestation, bytes)
            and hmac.compare_digest(value._attestation, expected)
        )
    except (TypeError, ValueError, AISafetyError):
        verified = False
    if not verified:
        raise AISafetyError(
            "A gateway-prepared AI context is required.",
            code="ai_context_not_prepared",
        )
    return value.canonical_json


def _prepared_context_attestation(
    *,
    purpose: str,
    canonical_json: str,
    input_sha256: str,
    field_paths: tuple[str, ...],
    pii_findings: dict[str, int],
    redaction_count: int,
    input_chars: int,
    estimated_input_tokens: int,
) -> bytes:
    """Bind every transport and audit field to one sanitized preflight result."""

    payload = _canonical_json(
        {
            "domain": _PREPARED_CONTEXT_ATTESTATION_DOMAIN,
            "purpose": purpose,
            "canonical_json": canonical_json,
            "input_sha256": input_sha256,
            "field_paths": list(field_paths),
            "pii_findings": pii_findings,
            "redaction_count": redaction_count,
            "input_chars": input_chars,
            "estimated_input_tokens": estimated_input_tokens,
        }
    ).encode("utf-8")
    return hmac.new(
        _PREPARED_CONTEXT_ATTESTATION_KEY,
        payload,
        hashlib.sha256,
    ).digest()


def configuration_fingerprint(configuration) -> str:
    payload = {
        "is_enabled": configuration.is_enabled,
        "research_enabled": configuration.research_enabled,
        "ai_scoring_enabled": configuration.ai_scoring_enabled,
        "provider": configuration.provider,
        "model": configuration.model,
        "reasoning_effort": configuration.reasoning_effort,
        "fallback_provider": configuration.fallback_provider,
        "fallback_model": configuration.fallback_model,
        "fallback_reasoning_effort": configuration.fallback_reasoning_effort,
        "allowed_ai_providers": _normalized_string_list(
            configuration.allowed_ai_providers
        ),
        "allowed_ai_purposes": _normalized_string_list(
            configuration.allowed_ai_purposes
        ),
        "pii_handling": configuration.pii_handling,
        "max_ai_input_chars": configuration.max_ai_input_chars,
        "max_ai_input_tokens": configuration.max_ai_input_tokens,
        "ai_audit_retention_days": configuration.ai_audit_retention_days,
        "icp_description": configuration.icp_description,
        "positive_signals": configuration.positive_signals,
        "negative_signals": configuration.negative_signals,
        "max_research_pages": configuration.max_research_pages,
        "website_timeout_seconds": configuration.website_timeout_seconds,
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def response_identifier_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def _normalized_string_list(value: Any) -> Any:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(value)
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise AISafetyError(
            "AI input cannot be serialized safely.", code="ai_input_invalid"
        ) from exc


def _leaf_paths(value: Any, path: str = "") -> set[str]:
    if isinstance(value, dict):
        if not value:
            return {path}
        paths: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str) or not key or "." in key:
                raise AISafetyError(
                    "AI input contains an invalid field name.", code="ai_input_invalid"
                )
            child = f"{path}.{key}" if path else key
            paths.update(_leaf_paths(item, child))
        return paths
    if isinstance(value, (list, tuple)):
        child = f"{path}[]"
        if not value:
            return {child}
        paths: set[str] = set()
        for item in value:
            paths.update(_leaf_paths(item, child))
        return paths
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise AISafetyError(
            "AI input contains an unsafe value.", code="ai_input_invalid"
        )
    return {path}


def _path_allowed(path: str, allowed: frozenset[str]) -> bool:
    if path in allowed:
        return True
    parts = path.split(".")
    for index in range(len(parts)):
        wildcard = ".".join(parts[:index] + ["*"] + parts[index + 1 :])
        if wildcard in allowed:
            return True
    return False


def _sanitize_value(
    value: Any,
    *,
    pii_handling: str,
    findings: dict[str, int],
) -> tuple[Any, int]:
    if isinstance(value, dict):
        result = {}
        total = 0
        for key, item in value.items():
            sanitized, count = _sanitize_value(
                item,
                pii_handling=pii_handling,
                findings=findings,
            )
            result[key] = sanitized
            total += count
        return result, total
    if isinstance(value, (list, tuple)):
        result = []
        total = 0
        for item in value:
            sanitized, count = _sanitize_value(
                item,
                pii_handling=pii_handling,
                findings=findings,
            )
            result.append(sanitized)
            total += count
        return result, total
    if not isinstance(value, str):
        return value, 0
    return _sanitize_text(value, pii_handling=pii_handling, findings=findings)


def _sanitize_text(
    text: str,
    *,
    pii_handling: str,
    findings: dict[str, int],
) -> tuple[str, int]:
    if (
        _SECRET_RE.search(text)
        or _STANDALONE_SECRET_RE.search(text)
        or _PRIVATE_KEY_RE.search(text)
        or _RECOVERY_KEY_RE.search(text)
    ):
        raise AISafetyError(
            "AI input contains credential material.",
            code="ai_sensitive_content_blocked",
        )
    if _CHINESE_ID_RE.search(text) or _contains_payment_card(text):
        raise AISafetyError(
            "AI input contains a high-risk identifier.",
            code="ai_sensitive_content_blocked",
        )

    patterns = (
        ("email", _EMAIL_RE, "[REDACTED_EMAIL]"),
        ("phone", _PHONE_RE, "[REDACTED_PHONE]"),
        ("name", _LABELED_NAME_RE, "[REDACTED_NAME]"),
        ("name", _CHINESE_NAME_RE, "[REDACTED_NAME]"),
        ("name", _CONTEXTUAL_NAME_RE, "[REDACTED_NAME]"),
        ("name", _CHINESE_CONTEXT_NAME_RE, "[REDACTED_NAME]"),
    )
    detected = 0
    sanitized = text
    for category, pattern, replacement in patterns:
        count = len(pattern.findall(sanitized))
        if not count:
            continue
        findings[category] = findings.get(category, 0) + count
        detected += count
        if pii_handling == "redact":
            sanitized = pattern.sub(replacement, sanitized)
    if detected and pii_handling == "block":
        raise AISafetyError(
            "AI input contains PII disallowed by tenant policy.",
            code="ai_pii_blocked",
        )
    return sanitized, detected if pii_handling == "redact" else 0


def _contains_payment_card(text: str) -> bool:
    for match in re.finditer(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text):
        digits = "".join(
            character for character in match.group() if character.isdigit()
        )
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return True
    return False


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
