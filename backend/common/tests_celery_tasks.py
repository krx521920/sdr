import pytest
from django.core import mail
from django.test.utils import override_settings

from accounts.models import Account
from cases.models import Case
from common.models import Comment
from common.tasks import (
    send_email_user_delete,
    send_email_user_mentions,
    send_email_user_status,
)
from contacts.models import Contact
from invoices.models import Invoice
from leads.models import Lead
from opportunity.models import Opportunity
from tasks.models import Task


CELERY_TEST_SETTINGS = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERY_BROKER_URL": "memory://",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


def _create_comment_target(target_type, admin_user, org):
    factories = {
        "accounts": lambda: Account.objects.create(
            name="Celery comment account", created_by=admin_user, org=org
        ),
        "contacts": lambda: Contact.objects.create(
            first_name="Celery",
            last_name="Contact",
            created_by=admin_user,
            org=org,
        ),
        "leads": lambda: Lead.objects.create(
            title="Celery comment lead", created_by=admin_user, org=org
        ),
        "opportunity": lambda: Opportunity.objects.create(
            name="Celery comment opportunity",
            stage="QUALIFICATION",
            created_by=admin_user,
            org=org,
        ),
        "cases": lambda: Case.objects.create(
            name="Celery comment case",
            status="New",
            priority="High",
            created_by=admin_user,
            org=org,
        ),
        "tasks": lambda: Task.objects.create(
            title="Celery comment task",
            status="New",
            priority="High",
            created_by=admin_user,
            org=org,
        ),
        "invoices": lambda: Invoice.objects.create(
            invoice_title="Celery comment invoice", org=org
        ),
    }
    return factories[target_type]()


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_user_status_and_delete_email_tasks(admin_user):
    status_result = send_email_user_status.apply(args=(admin_user.id,))
    delete_result = send_email_user_delete.apply(args=(admin_user.email,))

    assert status_result.successful(), status_result.result
    assert delete_result.successful(), delete_result.result
    assert len(mail.outbox) == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target_type",
    [
        "accounts",
        "contacts",
        "leads",
        "opportunity",
        "cases",
        "tasks",
        "invoices",
    ],
)
@override_settings(**CELERY_TEST_SETTINGS)
def test_user_mention_email_task(
    target_type, admin_user, admin_profile, regular_user, user_profile, org_a
):
    target = _create_comment_target(target_type, admin_user, org_a)
    comment = Comment.objects.create(
        content_object=target,
        comment="Please review @user",
        commented_by=admin_profile,
        org=org_a,
    )
    result = send_email_user_mentions.apply(
        args=(comment.id, target_type, str(org_a.id))
    )

    assert result.successful(), result.result
    assert len(mail.outbox) == 1
