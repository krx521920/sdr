import pytest
from django.core import mail
from django.test.utils import override_settings

from opportunity.models import Opportunity
from opportunity.tasks import send_email_to_assigned_user

CELERY_TEST_SETTINGS = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERY_BROKER_URL": "memory://",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_send_email_to_assigned_user(
    admin_user, admin_profile, user_profile, profile_b, org_a
):
    opportunity = Opportunity.objects.create(
        name="Celery opportunity",
        stage="QUALIFICATION",
        created_by=admin_user,
        org=org_a,
    )

    result = send_email_to_assigned_user.apply(
        args=(
            [admin_profile.id, user_profile.id, profile_b.id],
            opportunity.id,
            str(org_a.id),
        )
    )

    assert result.successful(), result.result
    assert len(mail.outbox) == 2
    assert profile_b.user.email not in {
        recipient for message in mail.outbox for recipient in message.to
    }
