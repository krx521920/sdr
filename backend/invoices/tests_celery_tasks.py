import pytest
from django.core import mail
from django.test.utils import override_settings

from accounts.models import Account
from contacts.models import Contact
from invoices.models import Invoice
from invoices.tasks import send_email, send_invoice_to_client

CELERY_TEST_SETTINGS = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERY_BROKER_URL": "memory://",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}


@pytest.mark.django_db
@override_settings(**CELERY_TEST_SETTINGS)
def test_invoice_email_tasks(admin_profile, user_profile, profile_b, org_a):
    account = Account.objects.create(name="Celery invoice account", org=org_a)
    contact = Contact.objects.create(
        first_name="Invoice",
        last_name="Recipient",
        email="invoice-recipient@example.com",
        org=org_a,
    )
    invoice = Invoice.objects.create(
        invoice_title="Celery invoice",
        account=account,
        contact=contact,
        client_name="Invoice Recipient",
        client_email=contact.email,
        currency="USD",
        org=org_a,
    )
    Invoice.objects.filter(pk=invoice.pk).update(created_by=admin_profile.user)

    assignment = send_email.apply(
        args=(
            invoice.id,
            [admin_profile.id, user_profile.id, profile_b.id],
            str(org_a.id),
        )
    )
    client_delivery = send_invoice_to_client.apply(
        args=(invoice.id, str(org_a.id)),
        kwargs={"include_pdf": False},
    )

    assert assignment.successful(), assignment.result
    assert client_delivery.successful(), client_delivery.result
    invoice.refresh_from_db()
    assert invoice.is_email_sent is True
    assert invoice.status == "Sent"
    assert len(mail.outbox) == 3
    assert profile_b.user.email not in {
        recipient for message in mail.outbox for recipient in message.to
    }
