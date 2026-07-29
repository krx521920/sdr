from django.contrib import admin

from automation.models import AutomationJob, AutomationJobAttempt


class AutomationJobAttemptInline(admin.TabularInline):
    model = AutomationJobAttempt
    extra = 0
    can_delete = False
    readonly_fields = (
        "attempt_number",
        "status",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AutomationJob)
class AutomationJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "org",
        "status",
        "attempt_count",
        "max_attempts",
        "scheduled_for",
        "created_at",
    )
    list_filter = ("status", "name", "queue")
    search_fields = ("id", "idempotency_key", "org__name")
    readonly_fields = (
        "id",
        "payload",
        "result",
        "attempt_count",
        "replay_count",
        "queued_at",
        "started_at",
        "completed_at",
        "last_error_code",
        "last_error_message",
        "created_at",
        "updated_at",
    )
    list_select_related = ("org",)
    inlines = (AutomationJobAttemptInline,)

    def has_add_permission(self, request):
        return False


@admin.register(AutomationJobAttempt)
class AutomationJobAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job",
        "org",
        "attempt_number",
        "status",
        "started_at",
        "finished_at",
    )
    list_filter = ("status",)
    search_fields = ("job__id", "job__idempotency_key", "org__name")
    readonly_fields = (
        "id",
        "job",
        "org",
        "attempt_number",
        "status",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False
