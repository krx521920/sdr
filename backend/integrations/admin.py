from django.contrib import admin

from integrations.models import (
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
