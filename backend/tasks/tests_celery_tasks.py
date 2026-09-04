import pytest
from django.core import mail
from django.test.utils import override_settings

from tasks.celery_tasks import send_email
from tasks.models import Task

CELERY_TEST_SETTINGS = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERY_BROKER_URL": "memory://",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_send_task_assignment_email(
    admin_user,
    regular_user,
    admin_profile,
    user_profile,
    profile_b,
    org_a,
):
    task = Task.objects.create(
        title="Celery task",
        status="New",
        priority="High",
        created_by=admin_user,
        org=org_a,
    )

    result = send_email.apply(
        args=(
            task.id,
            [admin_user.id, regular_user.id, profile_b.user_id],
            str(org_a.id),
        )
    )

    assert result.successful(), result.result
    assert len(mail.outbox) == 2
    assert profile_b.user.email not in {
        recipient for message in mail.outbox for recipient in message.to
    }
