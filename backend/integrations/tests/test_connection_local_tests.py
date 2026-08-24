import re
from unittest.mock import patch

import pytest
from django.db import connection as db_connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from automation.models import AutomationJob
from integrations.models import (
    ApolloConnection,
    LinkedInConnection,
    LinkedInInvitation,
    WhatsAppBusinessConnection,
    WhatsAppMessage,
    WhatsAppPhoneRoute,
)


APOLLO_TEST_URL = "/api/integrations/apollo/connection/test/"
WHATSAPP_TEST_URL = "/api/integrations/whatsapp/connection/test/"
LINKEDIN_TEST_URL = "/api/integrations/linkedin/connection/test/"
LOCAL_RESULT_FIELDS = {"code", "ok", "local_only"}


def _create_apollo_connection(org, *, active=True, ciphertext=None):
    connection = ApolloConnection(
        org=org,
        api_key_ciphertext="",
        api_key_hint="apollo-hint",
        is_active=active,
    )
    if ciphertext is None:
        connection.set_api_key("apollo-local-test-secret")
    else:
        connection.api_key_ciphertext = ciphertext
    connection.save()
    return connection


def _create_whatsapp_connection(
    org,
    *,
    active=True,
    ciphertext=None,
    phone_number_id="123456789",
):
    route = WhatsAppPhoneRoute.objects.create(
        org=org,
        phone_number_id=phone_number_id,
    )
    connection = WhatsAppBusinessConnection(
        org=org,
        route=route,
        access_token_ciphertext="",
        access_token_hint="wa-hint",
        is_active=active,
    )
    if ciphertext is None:
        connection.set_access_token("whatsapp-local-test-secret")
    else:
        connection.access_token_ciphertext = ciphertext
    connection.save()
    return connection


def _create_linkedin_connection(
    org,
    *,
    active=True,
    ciphertext=None,
    partner_access_confirmed=True,
):
    connection = LinkedInConnection(
        org=org,
        access_token_ciphertext="",
        access_token_hint="linkedin-hint",
        is_active=active,
        partner_access_confirmed=partner_access_confirmed,
    )
    if ciphertext is None:
        connection.set_access_token("linkedin-local-test-secret")
    else:
        connection.access_token_ciphertext = ciphertext
    connection.save()
    return connection


CONNECTION_CASES = (
    (APOLLO_TEST_URL, _create_apollo_connection),
    (WHATSAPP_TEST_URL, _create_whatsapp_connection),
    (LINKEDIN_TEST_URL, _create_linkedin_connection),
)


def _assert_result(response, *, status_code, code, ok=False):
    assert response.status_code == status_code, response.content
    assert set(response.json()) == LOCAL_RESULT_FIELDS
    assert response.json() == {"code": code, "ok": ok, "local_only": True}


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
@pytest.mark.parametrize(("url", "factory"), CONNECTION_CASES)
def test_local_connection_test_is_admin_only(user_client, org_a, url, factory):
    factory(org_a)

    response = user_client.post(url, {}, format="json")

    _assert_result(response, status_code=403, code="permission_denied")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
@pytest.mark.parametrize("url", [case[0] for case in CONNECTION_CASES])
def test_local_connection_test_reports_missing_configuration(admin_client, url):
    response = admin_client.post(url, {}, format="json")

    _assert_result(response, status_code=404, code="connection_missing")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
@pytest.mark.parametrize(("url", "factory"), CONNECTION_CASES)
def test_local_connection_test_reports_inactive_connection(
    admin_client,
    org_a,
    url,
    factory,
):
    factory(org_a, active=False)

    response = admin_client.post(url, {}, format="json")

    _assert_result(response, status_code=409, code="connection_inactive")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_whatsapp_local_test_requires_phone_number_identifier(admin_client, org_a):
    _create_whatsapp_connection(org_a, phone_number_id="")

    response = admin_client.post(WHATSAPP_TEST_URL, {}, format="json")

    _assert_result(response, status_code=400, code="required_identifier_missing")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
@pytest.mark.parametrize(("url", "factory"), CONNECTION_CASES)
def test_local_connection_test_reports_missing_credential(
    admin_client,
    org_a,
    url,
    factory,
):
    factory(org_a, ciphertext="")

    response = admin_client.post(url, {}, format="json")

    _assert_result(response, status_code=400, code="credential_missing")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
@pytest.mark.parametrize(("url", "factory"), CONNECTION_CASES)
def test_local_connection_test_hides_credential_decryption_failure(
    admin_client,
    org_a,
    url,
    factory,
):
    stored_ciphertext = "not-valid-fernet-ciphertext"
    factory(org_a, ciphertext=stored_ciphertext)

    response = admin_client.post(url, {}, format="json")

    _assert_result(response, status_code=400, code="credential_decryption_failed")
    serialized = response.content.decode("utf-8")
    assert stored_ciphertext not in serialized
    assert "hint" not in serialized.lower()
    assert "secret" not in serialized.lower()


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_linkedin_local_test_requires_confirmed_partner_access(admin_client, org_a):
    _create_linkedin_connection(org_a, partner_access_confirmed=False)

    response = admin_client.post(LINKEDIN_TEST_URL, {}, format="json")

    _assert_result(response, status_code=409, code="partner_access_not_confirmed")


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
@pytest.mark.parametrize(("url", "factory"), CONNECTION_CASES)
def test_local_connection_test_is_read_only_and_never_calls_providers(
    admin_client,
    org_a,
    url,
    factory,
):
    stored = factory(org_a)
    before = {
        field: getattr(stored, field)
        for field in (
            "updated_at",
            "is_active",
            "last_sync_at",
            "last_message_sent_at",
            "last_webhook_at",
            "last_invitation_sent_at",
        )
        if hasattr(stored, field)
    }
    object_counts = {
        model: model.objects.count()
        for model in (AutomationJob, WhatsAppMessage, LinkedInInvitation)
    }

    provider_call_error = AssertionError("local connection test called a provider")
    with (
        patch(
            "integrations.providers.apollo.client.ApolloClient.search_people",
            side_effect=provider_call_error,
        ),
        patch(
            "integrations.providers.whatsapp.client.WhatsAppCloudClient.send_template",
            side_effect=provider_call_error,
        ),
        patch(
            (
                "integrations.providers.linkedin.client."
                "LinkedInInvitationsClient.send_email_invitation"
            ),
            side_effect=provider_call_error,
        ),
        CaptureQueriesContext(db_connection) as queries,
    ):
        response = admin_client.post(url, {}, format="json")

    _assert_result(response, status_code=200, code="connection_ready", ok=True)
    write_queries = [
        query["sql"]
        for query in queries.captured_queries
        if re.match(r"^\s*(INSERT|UPDATE|DELETE)\b", query["sql"], re.IGNORECASE)
    ]
    assert write_queries == []
    stored.refresh_from_db()
    assert {
        field: getattr(stored, field)
        for field in before
    } == before
    assert {
        model: model.objects.count()
        for model in object_counts
    } == object_counts
