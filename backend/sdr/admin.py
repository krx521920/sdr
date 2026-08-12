from django.contrib import admin

from sdr.models import (
    LeadDelivery,
    LeadInspection,
    LeadIntake,
    LeadLifecycleEvent,
    LeadNurtureDelivery,
    LeadNurtureEnrollment,
    LeadNurtureInteraction,
    SDREmailProviderEvent,
    SDREmailSuppression,
    SDRIntelligenceSettings,
    SDRModelCredential,
    SDRNurtureSequence,
    SDRNurtureStep,
    SDROutboundCampaign,
    SDROutboundProspect,
    SDRResponseSettings,
    SDRRoutingRule,
    SDRRoutingRuleMember,
)


class SDROutboundProspectInline(admin.TabularInline):
    model = SDROutboundProspect
    extra = 0
    fields = ("company_name", "email", "job_title", "status", "intake")
    readonly_fields = fields
    show_change_link = True


@admin.register(SDROutboundCampaign)
class SDROutboundCampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "org",
        "status",
        "sequence",
        "daily_send_limit",
        "run_count",
        "owner",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("name", "description", "icp_description", "org__name")
    inlines = (SDROutboundProspectInline,)


@admin.register(SDROutboundProspect)
class SDROutboundProspectAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "email",
        "job_title",
        "campaign",
        "org",
        "status",
        "attempt_count",
        "promoted_at",
        "queued_at",
        "queued_run",
    )
    list_filter = ("status", "country", "industry")
    search_fields = ("company_name", "email", "first_name", "last_name")
    readonly_fields = (
        "dedupe_key",
        "attempt_count",
        "last_error_code",
        "last_error_message",
        "intake",
        "promoted_at",
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


class SDRNurtureStepInline(admin.StackedInline):
    model = SDRNurtureStep
    extra = 0


@admin.register(SDRNurtureSequence)
class SDRNurtureSequenceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "org",
        "priority",
        "is_active",
        "auto_enroll",
        "updated_at",
    )
    list_filter = ("is_active", "auto_enroll")
    search_fields = ("name", "org__name")
    inlines = (SDRNurtureStepInline,)


@admin.register(LeadNurtureEnrollment)
class LeadNurtureEnrollmentAdmin(admin.ModelAdmin):
    list_display = (
        "sequence",
        "intake",
        "org",
        "status",
        "current_step_position",
        "next_run_at",
        "enrolled_at",
    )
    list_filter = ("status", "sequence")
    search_fields = ("intake__source_record_id", "lead__email")
    readonly_fields = ("completed_at", "stop_reason")


@admin.register(LeadNurtureDelivery)
class LeadNurtureDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "enrollment",
        "step_position",
        "variant",
        "recipient",
        "status",
        "scheduled_for",
        "sent_at",
        "delivered_at",
        "bounced_at",
        "complained_at",
        "opened_at",
        "clicked_at",
        "reply_sentiment",
    )
    list_filter = ("status", "variant", "reply_sentiment")
    search_fields = ("recipient", "enrollment__intake__source_record_id")
    readonly_fields = (
        "subject_template",
        "body_template",
        "last_error_code",
        "last_error_message",
        "sent_at",
        "provider_message_id",
        "delivered_at",
        "bounced_at",
        "complained_at",
        "bounce_type",
        "bounce_subtype",
        "opened_at",
        "clicked_at",
        "open_count",
        "click_count",
        "last_clicked_url",
        "replied_at",
        "reply_message_id",
    )


@admin.register(LeadNurtureInteraction)
class LeadNurtureInteractionAdmin(admin.ModelAdmin):
    list_display = (
        "delivery",
        "event_type",
        "target_url",
        "occurred_at",
    )
    list_filter = ("event_type",)
    search_fields = ("delivery__recipient", "target_url")
    readonly_fields = (
        "delivery",
        "org",
        "event_type",
        "target_url",
        "target_hash",
        "visitor_hash",
        "occurred_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(SDREmailSuppression)
class SDREmailSuppressionAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "org",
        "reason",
        "source",
        "is_active",
        "suppressed_at",
        "released_at",
    )
    list_filter = ("is_active", "reason", "source")
    search_fields = ("email", "org__name")
    readonly_fields = ("suppressed_at", "released_at", "source_delivery", "details")


@admin.register(SDREmailProviderEvent)
class SDREmailProviderEventAdmin(admin.ModelAdmin):
    list_display = (
        "delivery",
        "provider",
        "event_type",
        "provider_event_id",
        "event_at",
    )
    list_filter = ("provider", "event_type")
    search_fields = ("provider_event_id", "delivery__recipient")
    readonly_fields = (
        "delivery",
        "org",
        "provider",
        "provider_event_id",
        "event_type",
        "event_at",
        "details",
    )

    def has_add_permission(self, request):
        return False


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
