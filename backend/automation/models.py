"""Durable, tenant-owned background job state and attempt history."""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from common.base import BaseOrgModel


class AutomationJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRY_SCHEDULED = "retry_scheduled", "Retry scheduled"
    SUCCEEDED = "succeeded", "Succeeded"
    DEAD_LETTER = "dead_letter", "Dead letter"
    CANCELLED = "cancelled", "Cancelled"


class AutomationAttemptStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class AutomationJob(BaseOrgModel):
    """One recoverable job execution, unique by tenant and idempotency key."""

    name = models.CharField(max_length=160)
    idempotency_key = models.CharField(max_length=255)
    queue = models.CharField(max_length=80, default="default")
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    status = models.CharField(
        max_length=24,
        choices=AutomationJobStatus.choices,
        default=AutomationJobStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(
        default=3,
        validators=[MinValueValidator(1)],
    )
    replay_count = models.PositiveIntegerField(default=0)
    scheduled_for = models.DateTimeField()
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    last_error_message = models.TextField(blank=True)

    class Meta:
        db_table = "automation_job"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "name", "idempotency_key"],
                name="unique_automation_job_key_per_org",
            )
        ]
        indexes = [
            models.Index(fields=["org", "status", "scheduled_for"]),
            models.Index(fields=["org", "name", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.name}:{self.idempotency_key}"


class AutomationJobAttempt(BaseOrgModel):
    """Immutable-ish audit record for one claimed execution attempt."""

    job = models.ForeignKey(
        AutomationJob,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16,
        choices=AutomationAttemptStatus.choices,
        default=AutomationAttemptStatus.RUNNING,
    )
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "automation_job_attempt"
        ordering = ("-attempt_number",)
        constraints = [
            models.UniqueConstraint(
                fields=["job", "attempt_number"],
                name="unique_automation_attempt_number",
            )
        ]
        indexes = [models.Index(fields=["org", "status", "-started_at"])]

    def clean(self) -> None:
        super().clean()
        if self.job_id and self.org_id and self.job.org_id != self.org_id:
            raise ValidationError("Automation attempt must belong to the job's org")

    def __str__(self) -> str:
        return f"{self.job_id}:{self.attempt_number}"
