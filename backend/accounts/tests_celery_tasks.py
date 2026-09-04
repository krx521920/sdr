import pytest
from django.core import mail
from django.test.utils import override_settings

from accounts.models import Account, AccountEmail, AccountEmailLog
from accounts.tasks import send_email, send_email_to_assigned_user
from contacts.models import Contact

CELERY_TEST_SETTINGS = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERY_BROKER_URL": "memory://",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_account_email_tasks(
    admin_user, admin_profile, user_profile, profile_b, org_a
):
    account = Account.objects.create(
        name="Celery account",
        created_by=admin_user,
        org=org_a,
    )
    contact = Contact.objects.create(
        first_name="Celery",
        last_name="Recipient",
        email="celery-recipient@example.com",
        created_by=admin_user,
        org=org_a,
    )
    account_email = AccountEmail.objects.create(
        message_subject="Celery account message",
        message_body="Hello {{ name }}",
        from_account=account,
        from_email="sender@example.com",
        org=org_a,
    )
    account_email.recipients.add(contact)

    delivery = send_email.apply(args=(account_email.id, str(org_a.id)))
    assignment = send_email_to_assigned_user.apply(
        args=(
            [admin_profile.id, user_profile.id, profile_b.id],
            account.id,
            str(org_a.id),
        )
    )

    assert delivery.successful(), delivery.result
    assert assignment.successful(), assignment.result
    assert AccountEmailLog.objects.filter(
        email=account_email, contact=contact, is_sent=True
    ).exists()
    assert len(mail.outbox) == 3
    assert profile_b.user.email not in {
        recipient for message in mail.outbox for recipient in message.to
    }
