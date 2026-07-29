"""Pipeline adapter coordinating deterministic enrichment, research, and AI scoring."""

import hashlib
import json
from dataclasses import replace

from django.utils import timezone

from sdr.domain import LeadCandidate, QualificationResult
from sdr.enrichment import EmailDomainEnricher
from sdr.intelligence.contracts import (
    PROMPT_VERSION,
    AIQualification,
    ModelProviderError,
)
from sdr.intelligence.gateway import ModelGateway, ModelGatewayError
from sdr.intelligence.research import (
    ResearchResult,
    WebsiteResearcher,
    WebsiteResearchError,
)
from sdr.models import (
    LeadInspection,
    LeadInspectionFallbackKind,
    LeadInspectionStatus,
    SDRIntelligenceSettings,
)
from sdr.scoring import RuleBasedLeadScorer


class LeadInspector:
    """One-use stateful adapter implementing both enrichment and scoring ports."""

    def __init__(
        self,
        *,
        intake,
        configuration: SDRIntelligenceSettings | None,
        researcher=None,
        qualifier=None,
    ):
        self.intake = intake
        self.configuration = configuration
        self.researcher = researcher or WebsiteResearcher()
        self.qualifier = qualifier
        self.email_enricher = EmailDomainEnricher()
        self.rule_scorer = RuleBasedLeadScorer()
        self.candidate: LeadCandidate | None = None
        self.research: ResearchResult | None = None
        self.ai_result: AIQualification | None = None
        self.errors: list[tuple[str, str]] = []
        self.provider_attempts: list[dict] = []
        self.inspection: LeadInspection | None = None

        if configuration and configuration.is_enabled:
            self.inspection, _ = LeadInspection.objects.update_or_create(
                org_id=intake.org_id,
                intake=intake,
                defaults={
                    "status": LeadInspectionStatus.RUNNING,
                    "website_url": "",
                    "source_urls": [],
                    "research_summary": "",
                    "research_facts": {},
                    "content_sha256": "",
                    "provider": "",
                    "model": "",
                    "prompt_version": "",
                    "configuration_sha256": "",
                    "provider_response_id": "",
                    "qualification_score": None,
                    "qualification_band": "",
                    "qualification_reasons": [],
                    "used_fallback": False,
                    "fallback_kind": "",
                    "provider_attempts": [],
                    "input_tokens": None,
                    "output_tokens": None,
                    "started_at": timezone.now(),
                    "completed_at": None,
                    "error_code": "",
                    "error_message": "",
                },
            )

    @classmethod
    def for_intake(cls, intake):
        configuration = SDRIntelligenceSettings.objects.filter(
            org_id=intake.org_id
        ).first()
        return cls(intake=intake, configuration=configuration)

    @property
    def enabled(self) -> bool:
        return bool(self.configuration and self.configuration.is_enabled)

    def enrich(self, candidate: LeadCandidate) -> LeadCandidate:
        candidate = self.email_enricher.enrich(candidate)
        self.candidate = candidate
        config = self.configuration
        if (
            not self.enabled
            or not config.research_enabled
            or not candidate.company.website
        ):
            return candidate
        try:
            self.research = self.researcher.research(
                candidate.company.website,
                max_pages=config.max_research_pages,
                timeout_seconds=config.website_timeout_seconds,
            )
        except WebsiteResearchError as exc:
            self.errors.append((exc.code, str(exc)))
            return candidate

        attributes = dict(candidate.attributes)
        attributes["company_research"] = {
            "summary": self.research.summary,
            "facts": dict(self.research.facts),
            "source_urls": list(self.research.source_urls),
            "content_sha256": self.research.content_sha256,
        }
        candidate = replace(candidate, attributes=attributes)
        self.candidate = candidate
        return candidate

    def score(self, candidate: LeadCandidate) -> QualificationResult:
        baseline = self.rule_scorer.score(candidate)
        config = self.configuration
        if not self.enabled or not config.ai_scoring_enabled:
            return baseline
        qualifier = self.qualifier or ModelGateway.for_configuration(config)
        try:
            self.ai_result = qualifier.qualify(
                org_id=candidate.org_id,
                candidate=candidate,
                baseline=baseline,
                research=self.research,
                icp_description=config.icp_description,
                positive_signals=config.positive_signals,
                negative_signals=config.negative_signals,
            )
        except ModelGatewayError as exc:
            self.provider_attempts = list(exc.attempts)
            self._record_provider_failures()
            return baseline
        except ModelProviderError as exc:
            self.provider_attempts = [
                {
                    "provider": config.provider,
                    "model": config.model,
                    "status": "failed",
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                }
            ]
            self.errors.append((exc.code, str(exc)))
            return baseline
        self.provider_attempts = list(self.ai_result.attempts)
        if not self.provider_attempts:
            self.provider_attempts = [
                {
                    "provider": self.ai_result.provider,
                    "model": self.ai_result.model or config.model,
                    "status": "completed",
                }
            ]
        self._record_provider_failures()
        return self.ai_result.qualification

    def _record_provider_failures(self) -> None:
        for attempt in self.provider_attempts:
            if attempt.get("status") != "failed":
                continue
            code = str(attempt.get("error_code") or "model_provider_failed")
            provider = str(attempt.get("provider") or "model provider")
            self.errors.append((code, f"{provider} qualification route failed."))

    def complete(self, qualification: QualificationResult) -> None:
        if self.inspection is None:
            return
        ai_enabled = bool(self.configuration.ai_scoring_enabled)
        if ai_enabled and self.ai_result is None:
            fallback_kind = LeadInspectionFallbackKind.RULES
        elif self.ai_result and self.ai_result.gateway_fallback_used:
            fallback_kind = LeadInspectionFallbackKind.MODEL
        else:
            fallback_kind = LeadInspectionFallbackKind.NONE
        metadata = dict(qualification.metadata)
        facts = dict(self.research.facts) if self.research else {}
        facts.update(metadata)
        if self.errors:
            facts["inspection_warnings"] = [
                {"code": code, "message": message[:500]}
                for code, message in self.errors
            ]
        summary = str(metadata.get("company_summary") or "")
        if not summary and self.research:
            summary = self.research.summary
        error_code, error_message = self.errors[0] if self.errors else ("", "")
        self.inspection.status = (
            LeadInspectionStatus.PARTIAL
            if self.errors
            else LeadInspectionStatus.COMPLETED
        )
        website_url = ""
        if self.research:
            website_url = self.research.website_url
        elif self.candidate:
            website_url = self.candidate.company.website or ""
        self.inspection.website_url = website_url
        self.inspection.source_urls = (
            list(self.research.source_urls) if self.research else []
        )
        self.inspection.research_summary = summary
        self.inspection.research_facts = facts
        self.inspection.content_sha256 = (
            self.research.content_sha256 if self.research else ""
        )
        self.inspection.provider = (
            self.ai_result.provider if self.ai_result else "rules"
        )
        self.inspection.model = (
            self.ai_result.model or self.configuration.model
            if self.ai_result
            else "rules-v1"
        )
        self.inspection.prompt_version = PROMPT_VERSION if self.ai_result else ""
        self.inspection.configuration_sha256 = self._configuration_fingerprint()
        self.inspection.provider_response_id = (
            self.ai_result.response_id if self.ai_result else ""
        )
        self.inspection.qualification_score = qualification.score
        self.inspection.qualification_band = qualification.band.value
        self.inspection.qualification_reasons = list(qualification.reasons)
        self.inspection.used_fallback = bool(fallback_kind)
        self.inspection.fallback_kind = fallback_kind
        self.inspection.provider_attempts = self.provider_attempts
        self.inspection.error_code = error_code
        self.inspection.error_message = error_message[:1000]
        self.inspection.input_tokens = (
            self.ai_result.input_tokens if self.ai_result else None
        )
        self.inspection.output_tokens = (
            self.ai_result.output_tokens if self.ai_result else None
        )
        self.inspection.completed_at = timezone.now()
        self.inspection.save()

    def _configuration_fingerprint(self) -> str:
        config = self.configuration
        payload = {
            "research_enabled": config.research_enabled,
            "ai_scoring_enabled": config.ai_scoring_enabled,
            "provider": config.provider,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "fallback_provider": config.fallback_provider,
            "fallback_model": config.fallback_model,
            "fallback_reasoning_effort": config.fallback_reasoning_effort,
            "icp_description": config.icp_description,
            "positive_signals": config.positive_signals,
            "negative_signals": config.negative_signals,
            "max_research_pages": config.max_research_pages,
            "website_timeout_seconds": config.website_timeout_seconds,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    def fail(self, exc: Exception) -> None:
        if self.inspection is None:
            return
        self.inspection.status = LeadInspectionStatus.FAILED
        self.inspection.error_code = "pipeline_failed"
        self.inspection.error_message = (str(exc) or exc.__class__.__name__)[:1000]
        self.inspection.used_fallback = True
        self.inspection.fallback_kind = LeadInspectionFallbackKind.RULES
        self.inspection.provider_attempts = self.provider_attempts
        self.inspection.configuration_sha256 = self._configuration_fingerprint()
        self.inspection.completed_at = timezone.now()
        self.inspection.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "used_fallback",
                "fallback_kind",
                "provider_attempts",
                "configuration_sha256",
                "completed_at",
                "updated_at",
            ]
        )
