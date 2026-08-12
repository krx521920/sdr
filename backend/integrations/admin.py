from django.contrib import admin

from integrations.models import (
    FacebookConversionEvent,
    FacebookConversionSettings,
    FacebookMessengerMessage,
    FacebookMessengerReply,
    FacebookOAuthSession,
    FacebookPageConnection,
    FacebookPageRoute,
)


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
