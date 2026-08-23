from django.contrib import admin

from integrations.models import (
    ApolloConnection,
    FacebookConversionEvent,
    FacebookConversionSettings,
    FacebookMessengerMessage,
    FacebookMessengerReply,
    FacebookOAuthSession,
    FacebookPageConnection,
    FacebookPageRoute,
    FeishuBaseConnection,
    FeishuBaseSync,
    LinkedInConnection,
    LinkedInInvitation,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppPhoneRoute,
)


@admin.register(FeishuBaseConnection)
class FeishuBaseConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "org",
        "app_id",
        "table_id",
        "is_active",
        "last_validated_at",
        "last_sync_at",
    )
    list_filter = ("is_active",)
    search_fields = ("org__name", "app_id", "app_token", "table_id")
    list_select_related = ("org",)
    readonly_fields = (
        "id",
        "app_secret_ciphertext",
        "app_secret_hint",
        "last_validated_at",
        "last_sync_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FeishuBaseSync)
class FeishuBaseSyncAdmin(admin.ModelAdmin):
    list_display = (
        "intake",
        "org",
        "status",
        "attempt_count",
        "record_id",
        "synced_at",
    )
    list_filter = ("status",)
    search_fields = ("record_id", "intake__source_record_id", "org__name")
    list_select_related = ("org", "connection", "intake")
    readonly_fields = (
        "id",
        "org",
        "connection",
        "intake",
        "status",
        "record_id",
        "destination_sha256",
        "payload_sha256",
        "attempt_count",
        "synced_field_names",
        "error_code",
        "error_message",
        "last_attempted_at",
        "synced_at",
        "failed_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(LinkedInConnection)
class LinkedInConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "org",
        "is_active",
        "partner_access_confirmed",
        "access_token_hint",
        "last_invitation_sent_at",
    )
    list_filter = ("is_active", "partner_access_confirmed")
    search_fields = ("org__name", "access_token_hint")
    list_select_related = ("org",)
    readonly_fields = (
        "id",
        "access_token_ciphertext",
        "access_token_hint",
        "last_invitation_sent_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(LinkedInInvitation)
class LinkedInInvitationAdmin(admin.ModelAdmin):
    list_display = (
        "recipient",
        "campaign",
        "campaign_run",
        "status",
        "attempt_count",
        "sent_at",
    )
    list_filter = ("status",)
    search_fields = (
        "recipient",
        "provider_invitation_id",
        "campaign__name",
        "prospect__company_name",
        "org__name",
    )
    list_select_related = ("org", "connection", "campaign", "prospect")
    readonly_fields = (
        "id",
        "org",
        "connection",
        "campaign",
        "prospect",
        "campaign_run",
        "recipient",
        "message_body",
        "status",
        "attempt_count",
        "provider_invitation_id",
        "error_code",
        "error_message",
        "sent_at",
        "failed_at",
        "provider_status_snapshot",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(ApolloConnection)
class ApolloConnectionAdmin(admin.ModelAdmin):
    list_display = ("org", "is_active", "api_key_hint", "last_sync_at")
    list_filter = ("is_active",)
    search_fields = ("org__name", "api_key_hint")
    list_select_related = ("org",)
    readonly_fields = (
        "id",
        "api_key_ciphertext",
        "api_key_hint",
        "last_sync_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FacebookPageRoute)
class FacebookPageRouteAdmin(admin.ModelAdmin):
    list_display = ("page_id", "org", "created_at")
    search_fields = ("page_id", "org__name")
    list_select_related = ("org",)
    readonly_fields = ("id", "created_at")


@admin.register(FacebookPageConnection)
class FacebookPageConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "page_name",
        "page_id",
        "org",
        "is_active",
        "token_expires_at",
        "last_webhook_at",
        "messenger_enabled",
        "messenger_auto_reply_enabled",
        "last_message_at",
        "last_message_reply_at",
    )
    list_filter = ("is_active",)
    search_fields = ("page_name", "route__page_id", "org__name")
    list_select_related = ("route", "org")
    readonly_fields = (
        "id",
        "access_token_ciphertext",
        "access_token_hint",
        "created_at",
        "updated_at",
        "last_webhook_at",
        "last_message_at",
        "last_message_reply_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FacebookOAuthSession)
class FacebookOAuthSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "org",
        "initiated_by_profile",
        "status",
        "expires_at",
        "completed_at",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("id", "org__name", "initiated_by_profile__user__email")
    list_select_related = ("org", "initiated_by_profile__user")
    readonly_fields = (
        "id",
        "pages_snapshot",
        "page_tokens_ciphertext",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "completed_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FacebookConversionSettings)
class FacebookConversionSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "org",
        "is_enabled",
        "pixel_id",
        "access_token_hint",
        "last_event_sent_at",
        "updated_at",
    )
    list_filter = ("is_enabled",)
    search_fields = ("org__name", "pixel_id")
    list_select_related = ("org",)
    readonly_fields = (
        "id",
        "access_token_ciphertext",
        "access_token_hint",
        "last_event_sent_at",
        "created_at",
        "updated_at",
    )


@admin.register(FacebookConversionEvent)
class FacebookConversionEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_name",
        "leadgen_id",
        "org",
        "status",
        "event_time",
        "sent_at",
    )
    list_filter = ("status", "event_name")
    search_fields = ("leadgen_id", "event_key", "provider_trace_id", "org__name")
    list_select_related = ("org", "intake", "crm_lead")
    readonly_fields = (
        "id",
        "org",
        "intake",
        "crm_lead",
        "leadgen_id",
        "event_name",
        "event_key",
        "event_time",
        "pixel_id",
        "lead_event_source",
        "test_event_code",
        "status",
        "provider_events_received",
        "provider_trace_id",
        "error_code",
        "error_message",
        "last_attempted_at",
        "sent_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FacebookMessengerMessage)
class FacebookMessengerMessageAdmin(admin.ModelAdmin):
    list_display = (
        "message_id",
        "page_id",
        "org",
        "status",
        "occurred_at",
        "processed_at",
    )
    list_filter = ("status",)
    search_fields = ("message_id", "page_id", "sender_psid", "org__name")
    list_select_related = ("org", "connection", "intake")
    readonly_fields = (
        "id",
        "org",
        "connection",
        "intake",
        "page_id",
        "sender_psid",
        "message_id",
        "body",
        "attachment_types",
        "occurred_at",
        "status",
        "error_code",
        "error_message",
        "processed_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FacebookMessengerReply)
class FacebookMessengerReplyAdmin(admin.ModelAdmin):
    list_display = (
        "page_id",
        "recipient_psid",
        "kind",
        "org",
        "status",
        "attempt_count",
        "sent_at",
    )
    list_filter = ("status", "kind")
    search_fields = (
        "page_id",
        "recipient_psid",
        "provider_message_id",
        "client_request_id",
        "org__name",
    )
    list_select_related = ("org", "connection", "trigger_message")
    readonly_fields = (
        "id",
        "org",
        "connection",
        "trigger_message",
        "page_id",
        "recipient_psid",
        "kind",
        "client_request_id",
        "body",
        "status",
        "attempt_count",
        "provider_message_id",
        "error_code",
        "error_message",
        "sent_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(WhatsAppPhoneRoute)
class WhatsAppPhoneRouteAdmin(admin.ModelAdmin):
    list_display = ("phone_number_id", "org", "created_at")
    search_fields = ("phone_number_id", "org__name")
    list_select_related = ("org",)
    readonly_fields = ("id", "created_at")


@admin.register(WhatsAppBusinessConnection)
class WhatsAppBusinessConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "display_phone_number",
        "phone_number_id",
        "org",
        "is_active",
        "last_message_sent_at",
        "last_webhook_at",
    )
    list_filter = ("is_active",)
    search_fields = ("display_phone_number", "route__phone_number_id", "org__name")
    list_select_related = ("route", "org")
    readonly_fields = (
        "id",
        "access_token_ciphertext",
        "access_token_hint",
        "last_message_sent_at",
        "last_webhook_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = (
        "recipient",
        "campaign",
        "campaign_run",
        "status",
        "attempt_count",
        "sent_at",
        "delivered_at",
        "read_at",
    )
    list_filter = ("status", "template_language")
    search_fields = (
        "recipient",
        "provider_message_id",
        "campaign__name",
        "prospect__company_name",
        "org__name",
    )
    list_select_related = ("org", "connection", "campaign", "prospect")
    readonly_fields = (
        "id",
        "org",
        "connection",
        "campaign",
        "prospect",
        "campaign_run",
        "recipient",
        "template_name",
        "template_language",
        "status",
        "attempt_count",
        "provider_message_id",
        "error_code",
        "error_message",
        "sent_at",
        "delivered_at",
        "read_at",
        "failed_at",
        "provider_status_snapshot",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False
