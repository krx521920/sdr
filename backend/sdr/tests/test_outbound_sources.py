import pytest
from django.test import override_settings

from integrations.models import ApolloConnection
from integrations.providers.apollo.client import ApolloClient
from sdr.models import SDROutboundCampaign, SDROutboundProspect, SDROutboundSource
from sdr.sources import APOLLO_PERSON_URL, _sync_apollo_source


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.call = None

    def request(self, method, url, *, headers, params, timeout):
        self.call = (method, url, headers, params, timeout)
        return FakeResponse({"people": [], "pagination": {"total_entries": 0}})


def test_apollo_client_uses_api_key_and_official_people_search_parameters():
    session = FakeSession()
    client = ApolloClient(api_key="apollo-secret", session=session)

    client.search_people(
        filters={
            "person_titles": ["VP Sales"],
            "organization_domains": ["example.com"],
            "unknown": ["ignored"],
        },
        page=2,
        per_page=100,
    )

    method, url, headers, params, timeout = session.call
    assert method == "POST"
    assert url == "https://api.apollo.io/api/v1/mixed_people/api_search"
    assert headers["x-api-key"] == "apollo-secret"
    assert params == {
        "person_titles[]": ["VP Sales"],
        "q_organization_domains_list[]": ["example.com"],
        "page": 2,
        "per_page": 100,
    }
    assert timeout == 15.0


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_apollo_connection_and_source_api_enforce_credit_acknowledgement(
    admin_client,
    org_a,
):
    configured = admin_client.put(
        "/api/integrations/apollo/connection/",
        {"api_key": "service-api-key", "is_active": True},
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    connection = ApolloConnection.objects.get(org=org_a)
    assert connection.api_key_ciphertext != "service-api-key"
    assert connection.get_api_key() == "service-api-key"
    assert configured.json()["api_key_configured"] is True
    assert "api_key" not in configured.json()

    campaign = SDROutboundCampaign.objects.create(org=org_a, name="Apollo buyers")
    source_url = f"/api/sdr/outbound/campaigns/{campaign.id}/sources/"
    rejected = admin_client.post(
        source_url,
        {
            "name": "Manufacturing leaders",
            "is_active": True,
            "search_filters": {"person_titles": ["VP Operations"]},
        },
        format="json",
    )
    assert rejected.status_code == 400
    assert "enrichment_credits_acknowledged" in rejected.json()

    created = admin_client.post(
        source_url,
        {
            "name": "Manufacturing leaders",
            "is_active": True,
            "search_filters": {
                "person_titles": ["VP Operations"],
                "organization_locations": ["Germany"],
            },
            "max_results_per_sync": 10,
            "enrichment_credits_acknowledged": True,
        },
        format="json",
    )
    assert created.status_code == 201, created.json()
    assert created.json()["next_sync_at"] is not None
    approval_intent = admin_client.post(
        f"/api/sdr/outbound/sources/{created.json()['id']}/sync/",
        format="json",
    )
    assert approval_intent.status_code == 200, approval_intent.json()
    assert approval_intent.json()["status"] == "approval_required"
    assert approval_intent.json()["intent"]["action"] == "search_people"
    assert approval_intent.json()["intent"]["units"] == 1


class FakeApolloClient:
    def __init__(self):
        self.enriched_ids = []

    def search_people(self, *, filters, page, per_page):
        assert filters == {"person_titles": ["CTO"]}
        assert page == 1
        assert per_page == 25
        return {
            "people": [
                {"id": "existing", "first_name": "Old"},
                {"id": "new-one", "first_name": "Ada"},
                {"id": "new-two", "first_name": "Grace"},
            ],
            "pagination": {"total_entries": 51},
        }

    def enrich_person(self, *, person_id):
        self.enriched_ids.append(person_id)
        return {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "title": "CTO",
            "linkedin_url": "https://linkedin.com/in/ada",
            "country": "United Kingdom",
            "organization": {
                "name": "Analytical Engines",
                "primary_domain": "analytical.example",
                "industry": "Industrial automation",
            },
        }


@pytest.mark.django_db
def test_source_sync_skips_existing_ids_before_enrichment_and_honors_limit(org_a):
    campaign = SDROutboundCampaign.objects.create(org=org_a, name="Apollo sync")
    SDROutboundProspect.objects.create(
        org=org_a,
        campaign=campaign,
        company_name="Already imported",
        email="old@example.com",
        source_url=APOLLO_PERSON_URL.format(person_id="existing"),
        dedupe_key="email:old@example.com",
    )
    source = SDROutboundSource.objects.create(
        org=org_a,
        campaign=campaign,
        name="CTO source",
        search_filters={"person_titles": ["CTO"]},
        max_results_per_sync=1,
        enrichment_credits_acknowledged=True,
    )
    client = FakeApolloClient()

    stats = _sync_apollo_source(source=source, client=client)

    assert client.enriched_ids == ["new-one"]
    assert stats == {
        "page": 1,
        "next_page": 2,
        "searched": 3,
        "enrichment_requests": 1,
        "created": 1,
        "duplicates": 1,
        "invalid": 0,
        "total_entries": 51,
    }
    prospect = SDROutboundProspect.objects.get(
        source_url=APOLLO_PERSON_URL.format(person_id="new-one")
    )
    assert prospect.company_name == "Analytical Engines"
    assert prospect.email == "ada@example.com"
    assert prospect.website == "https://analytical.example"
    assert prospect.country == "GB"
