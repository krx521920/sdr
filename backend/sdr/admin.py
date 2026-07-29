from django.contrib import admin

from sdr.models import LeadIntake, SDRRoutingRule, SDRRoutingRuleMember


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
