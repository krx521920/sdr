from unittest.mock import patch

import pytest
from django.core import mail
from django.test.utils import override_settings

from leads.models import Lead
from leads.tasks import (
    create_lead_from_file,
    send_email,
    send_email_to_assigned_user,
    send_lead_assigned_emails,
)

CELERY_TEST_SETTINGS = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERY_BROKER_URL": "memory://",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_lead_email_tasks(
    admin_user, admin_profile, user_profile, profile_b, org_a
):
    lead = Lead.objects.create(
        title="Celery lead",
        first_name="Celery",
        last_name="Lead",
        email="celery-lead@example.com",
        status="assigned",
        created_by=admin_user,
        org=org_a,
    )

    assignment = send_email_to_assigned_user.apply(
        args=(
            [admin_profile.id, user_profile.id, profile_b.id],
            lead.id,
            str(org_a.id),
        )
    )
    direct_email = send_email.apply(
        args=("Celery subject", "<p>Celery body</p>"),
        kwargs={"recipients": [admin_user.email, user_profile.user.email]},
    )

    with patch("leads.tasks.send_email.delay") as dispatch_email:
        assigned_emails = send_lead_assigned_emails.apply(
            args=(
                lead.id,
                [admin_profile.id, user_profile.id, profile_b.id],
                "https://example.com",
                str(org_a.id),
            )
        )

    assert assignment.successful(), assignment.result
    assert direct_email.successful(), direct_email.result
    assert assigned_emails.successful(), assigned_emails.result
    assert dispatch_email.call_count == 2
    assert len(mail.outbox) == 3
    assert profile_b.user.email not in {
        recipient for message in mail.outbox for recipient in message.to
    }


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_create_lead_from_file_task_accepts_current_profile_id(admin_profile, org_a):
    row = {
        "title": "Imported tenant lead",
        "first name": "Import",
        "last name": "Owner",
        "email": "imported@example.com",
    }
    result = create_lead_from_file.apply(
        args=([row], [], admin_profile.id, "csv", str(org_a.id))
    )

    assert result.successful(), result.result
    lead = Lead.objects.get(title=row["title"], org=org_a)
    assert lead.created_by == admin_profile.user
