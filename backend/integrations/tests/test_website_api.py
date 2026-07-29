import pytest
from django.test import override_settings


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_website_endpoint_keeps_contract_after_module_move(admin_client):
    payload = {
        "source_record_id": "website-42",
        "first_name": "Ada",
        "email": "ada@example.com",
        "company_name": "Acme",
    }

    created = admin_client.post("/api/sdr/intake/website/", payload, format="json")
    replayed = admin_client.post("/api/sdr/intake/website/", payload, format="json")

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["lead_id"] == created.json()["lead_id"]
