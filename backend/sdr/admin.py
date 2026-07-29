from django.contrib import admin

from sdr.models import (
    LeadDelivery,
    LeadInspection,
    LeadIntake,
    LeadLifecycleEvent,
    SDRIntelligenceSettings,
    SDRModelCredential,
    SDRResponseSettings,
    SDRRoutingRule,
    SDRRoutingRuleMember,
)


@admin.register(SDRResponseSettings)
class SDRResponseSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "org",
        "acknowledgement_email_enabled",
        "sales_in_app_enabled",
        "feishu_enabled",
        "response_sla_seconds",
        "updated_at",
    )
    exclude = ("feishu_webhook_ciphertext",)
    readonly_fields = ("feishu_webhook_hint",)


@admin.register(LeadLifecycleEvent)
class LeadLifecycleEventAdmin(admin.ModelAdmin):
    list_display = ("intake", "org", "event_type", "event_key", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("intake__source_record_id", "event_key")
    readonly_fields = ("data",)


@admin.register(LeadDelivery)
class LeadDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "intake",
        "org",
        "kind",
        "recipient",
        "status",
        "attempt_count",
        "sent_at",
    )
    list_filter = ("kind", "status")
    search_fields = ("intake__source_record_id", "recipient")
    readonly_fields = ("last_error_code", "last_error_message", "sent_at")


@admin.register(SDRIntelligenceSettings)
class SDRIntelligenceSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "org",
        "is_enabled",
        "research_enabled",
        "ai_scoring_enabled",
        "provider",
        "model",
        "fallback_provider",
    )
    list_filter = (
        "is_enabled",
        "research_enabled",
        "ai_scoring_enabled",
        "provider",
        "fallback_provider",
    )


@admin.register(SDRModelCredential)
class SDRModelCredentialAdmin(admin.ModelAdmin):
    list_display = ("org", "provider", "api_key_hint", "is_active", "updated_at")
    list_filter = ("provider", "is_active")
    exclude = ("api_key_ciphertext",)
    readonly_fields = ("api_key_hint",)

    def has_add_permission(self, request):
        return False


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
        "fallback_kind",
        "created_at",
    )
    list_filter = (
        "status",
        "qualification_band",
        "provider",
        "used_fallback",
        "fallback_kind",
    )
    search_fields = ("intake__source_record_id", "website_url")
    readonly_fields = (
        "source_urls",
        "research_facts",
        "qualification_reasons",
        "provider_attempts",
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
