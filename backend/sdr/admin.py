from django.contrib import admin

from sdr.models import (
    LeadInspection,
    LeadIntake,
    SDRIntelligenceSettings,
    SDRRoutingRule,
    SDRRoutingRuleMember,
)


@admin.register(SDRIntelligenceSettings)
class SDRIntelligenceSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "org",
        "is_enabled",
        "research_enabled",
        "ai_scoring_enabled",
        "model",
    )
    list_filter = ("is_enabled", "research_enabled", "ai_scoring_enabled")


@admin.register(LeadInspection)
class LeadInspectionAdmin(admin.ModelAdmin):
    list_display = (
        "intake",
        "org",
        "status",
        "qualification_score",
        "qualification_band",
        "provider",
        "model",
        "used_fallback",
        "created_at",
    )
    list_filter = ("status", "qualification_band", "provider", "used_fallback")
    search_fields = ("intake__source_record_id", "website_url")
    readonly_fields = (
        "source_urls",
        "research_facts",
        "qualification_reasons",
        "provider_response_id",
        "configuration_sha256",
    )


class SDRRoutingRuleMemberInline(admin.TabularInline):
    model = SDRRoutingRuleMember
    extra = 0


@admin.register(SDRRoutingRule)
class SDRRoutingRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "org", "priority", "strategy", "is_active")
    list_filter = ("strategy", "is_active")
    search_fields = ("name", "org__name")
    inlines = (SDRRoutingRuleMemberInline,)


@admin.register(LeadIntake)
class LeadIntakeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "source_record_id",
        "org",
        "status",
        "attempt_count",
        "qualification_score",
        "crm_lead",
        "routing_rule",
        "created_at",
    )
    list_filter = ("source", "status", "qualification_band", "crm_created")
    search_fields = ("source_record_id", "crm_lead__email", "crm_lead__phone")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "processed_at",
        "raw_payload",
        "normalized_payload",
        "error_message",
    )
    list_select_related = (
        "org",
        "crm_lead",
        "assigned_profile",
        "routing_rule",
    )
    date_hierarchy = "created_at"
