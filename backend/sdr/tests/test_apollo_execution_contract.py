from datetime import timedelta
from uuid import uuid4

import pytest
import requests
from django.test import override_settings
from django.utils import timezone

from automation.errors import PermanentJobError
from automation.models import AutomationJob, AutomationJobStatus
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    mark_execution_sending,
    mark_provider_accepted,
    reserve_execution,
    resolve_unknown_execution,
)
from integrations.models import (
    ApolloConnection,
    ChannelExecutionControl,
    ExecutionChannel,
    ExternalExecutionRequest,
    ExternalRequestStatus,
    OrganizationExecutionControl,
)
from integrations.providers.apollo import ApolloClient
from integrations.providers.sdr_adapters import ApolloClientAdapter
from matching.models import (
    EvidenceSource,
    PersonImportBatch,
    PersonImportBatchStatus,
)
from sdr.models import (
    ApolloCandidateStatus,
    SDRApolloCandidate,
    SDROutboundCampaign,
    SDROutboundSource,
)
from sdr.provider_ports import ProviderAdapterError
from sdr.sources import (
    APOLLO_CANDIDATE_ENRICH_JOB,
    _execute_approved_apollo_enrichment,
    _execute_approved_apollo_search,
    _payload_hash,
    _sync_apollo_source,
    apollo_enrichment_execution_intent,
    apollo_search_execution_intent,
    persist_apollo_enrichment_import,
    process_apollo_candidate_enrichment_job,
    process_outbound_source_sync_job,
    reconcile_apollo_candidate_states,
)


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse(
            {
                "people": [
                    {
                        "id": "apollo-person-1",
                        "first_name": "Must not persist",
                        "email": "must-not-persist@example.com",
                    }
                ],
                "pagination": {"total_entries": 1},
            }
        )
        self.error = error
        self.calls = []

    def request(self, method, url, *, headers, params, timeout):
        self.calls.append((method, url, headers, params, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def _source(org):
    campaign = SDROutboundCampaign.objects.create(org=org, name="Safe Apollo search")
    return SDROutboundSource.objects.create(
        org=org,
        campaign=campaign,
        name="Exact CTO search",
        search_filters={"person_titles": ["CTO"]},
        max_results_per_sync=1,
        enrichment_credits_acknowledged=True,
    )


def _enable_apollo(org, admin_profile, *, target_identifier):
    configure_organization_execution(
        org=org,
        actor=admin_profile,
        enabled=True,
        daily_limit=20,
    )
    configure_channel(
        org=org,
        actor=admin_profile,
        channel=ExecutionChannel.APOLLO,
        enabled=True,
        test_mode=True,
        daily_limit=20,
        per_execution_limit=1,
    )
    return add_test_target(
        org=org,
        actor=admin_profile,
        channel=ExecutionChannel.APOLLO,
        identifier=target_identifier,
        safe_label="Dedicated Apollo contract target",
    )


def _reserve_search(source, admin_profile):
    intent = apollo_search_execution_intent(source)
    _enable_apollo(
        source.org,
        admin_profile,
        target_identifier=intent.test_target_identifier,
    )
    approval = issue_execution_approval(
        org=source.org,
        approved_by=admin_profile,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
    )
    return reserve_execution(
        org=source.org,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    ).request


def _candidate_with_reserved_enrichment(source, admin_profile):
    search_request = _reserve_search(source, admin_profile)
    _execute_approved_apollo_search(
        source=source,
        client=ApolloClientAdapter(
            ApolloClient(api_key="secret", session=FakeSession())
        ),
        execution_request_id=search_request.id,
    )
    candidate = SDRApolloCandidate.objects.get(org=source.org, source=source)
    intent = apollo_enrichment_execution_intent(candidate)
    add_test_target(
        org=source.org,
        actor=admin_profile,
        channel=ExecutionChannel.APOLLO,
        identifier=intent.test_target_identifier,
        safe_label="Dedicated Apollo enrichment target",
    )
    approval = issue_execution_approval(
        org=source.org,
        approved_by=admin_profile,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
    )
    request = reserve_execution(
        org=source.org,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    ).request
    candidate.enrichment_request = request
    candidate.status = ApolloCandidateStatus.ENRICHMENT_RESERVED
    candidate.save(update_fields=["enrichment_request", "status", "updated_at"])
    return candidate, request


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_search_without_exact_execution_request_fails_before_provider_io(org_a):
    source = _source(org_a)
    session = FakeSession()
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    with pytest.raises(ProviderAdapterError) as exc_info:
        _sync_apollo_source(source=source, client=client)

    assert exc_info.value.error_code == "apollo_search_approval_required"
    assert session.calls == []


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
    ROOT_URLCONF="integrations.tests.urls",
)
def test_one_approved_search_consumes_one_unit_and_persists_only_safe_candidate_state(
    org_a,
    admin_profile,
    admin_client,
):
    source = _source(org_a)
    request = _reserve_search(source, admin_profile)
    session = FakeSession()
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    result = _execute_approved_apollo_search(
        source=source,
        client=client,
        execution_request_id=request.id,
    )

    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.DELIVERED
    assert result["status"] == "awaiting_enrichment_approval"
    assert result["candidate_count"] == 1
    assert result["enrichment_requests"] == 0
    assert len(session.calls) == 1
    candidate = SDRApolloCandidate.objects.get(org=org_a, source=source)
    assert candidate.status == ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL
    assert candidate.get_provider_person_id() == "apollo-person-1"
    assert candidate.provider_person_id_ciphertext != "apollo-person-1"
    assert "Must not persist" not in candidate.safe_label
    assert "must-not-persist" not in candidate.safe_label
    assert (
        ChannelExecutionControl.objects.get(
            org=org_a, channel=ExecutionChannel.APOLLO
        ).consumed_units
        == 1
    )
    assert OrganizationExecutionControl.objects.get(org=org_a).consumed_units == 1

    projection = admin_client.get(
        f"/api/sdr/outbound/sources/{source.id}/apollo-candidates/"
    )
    assert projection.status_code == 200, projection.json()
    projection_text = str(projection.json())
    assert "apollo-person-1" not in projection_text
    assert "Must not persist" not in projection_text
    assert "must-not-persist@example.com" not in projection_text
    assert projection.json()["results"][0]["enrichment_intent"]["action"] == (
        "enrich_person"
    )

    with pytest.raises(ProviderAdapterError) as exc_info:
        _execute_approved_apollo_search(
            source=source,
            client=client,
            execution_request_id=request.id,
        )
    assert exc_info.value.error_code == "apollo_execution_not_replayable"
    assert len(session.calls) == 1


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_transport_uncertainty_consumes_credit_and_never_replays(
    org_a,
    admin_profile,
):
    source = _source(org_a)
    request = _reserve_search(source, admin_profile)
    session = FakeSession(error=requests.Timeout("provider outcome unknown"))
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    with pytest.raises(ProviderAdapterError) as exc_info:
        _execute_approved_apollo_search(
            source=source,
            client=client,
            execution_request_id=request.id,
        )
    assert exc_info.value.error_code == "apollo_transport_error"
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert len(session.calls) == 1
    assert (
        ChannelExecutionControl.objects.get(
            org=org_a, channel=ExecutionChannel.APOLLO
        ).consumed_units
        == 1
    )

    with pytest.raises(ProviderAdapterError) as replay:
        _execute_approved_apollo_search(
            source=source,
            client=client,
            execution_request_id=request.id,
        )
    assert replay.value.error_code == "apollo_execution_not_replayable"
    assert len(session.calls) == 1


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_explicit_apollo_4xx_fails_and_releases_reserved_unit(org_a, admin_profile):
    source = _source(org_a)
    request = _reserve_search(source, admin_profile)
    session = FakeSession(
        response=FakeResponse({"message": "bad search"}, status_code=400)
    )
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    with pytest.raises(ProviderAdapterError) as exc_info:
        _execute_approved_apollo_search(
            source=source,
            client=client,
            execution_request_id=request.id,
        )
    assert exc_info.value.error_code == "apollo_http_400"
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.APOLLO,
    )
    assert (control.reserved_units, control.consumed_units) == (0, 0)


@pytest.mark.django_db
@pytest.mark.parametrize("status_code", [408, 425, 429, 500])
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_uncertain_http_status_consumes_credit_and_is_not_refunded(
    org_a,
    admin_profile,
    status_code,
):
    source = _source(org_a)
    request = _reserve_search(source, admin_profile)
    sensitive_echo = "apollo-person-1 must-not-leak@example.com"
    session = FakeSession(
        response=FakeResponse({"message": sensitive_echo}, status_code=status_code)
    )
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    with pytest.raises(ProviderAdapterError) as exc_info:
        _execute_approved_apollo_search(
            source=source,
            client=client,
            execution_request_id=request.id,
        )

    assert exc_info.value.error_code == f"apollo_http_{status_code}"
    assert sensitive_echo not in str(exc_info.value)
    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    control = ChannelExecutionControl.objects.get(
        org=org_a,
        channel=ExecutionChannel.APOLLO,
    )
    assert (control.reserved_units, control.consumed_units) == (0, 1)


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_local_candidate_write_failure_marks_provider_outcome_unknown(
    org_a,
    admin_profile,
    monkeypatch,
):
    source = _source(org_a)
    request = _reserve_search(source, admin_profile)
    session = FakeSession()
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    def fail_local_write(**kwargs):
        raise RuntimeError("local persistence failed")

    monkeypatch.setattr(
        "sdr.sources._persist_apollo_search_candidates",
        fail_local_write,
    )
    with pytest.raises(RuntimeError, match="local persistence failed"):
        _execute_approved_apollo_search(
            source=source,
            client=client,
            execution_request_id=request.id,
        )

    request.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert len(session.calls) == 1


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_enriched_profile_uses_provider_person_import_ledger(
    org_a,
    admin_profile,
):
    source = _source(org_a)
    search_request = _reserve_search(source, admin_profile)
    search_session = FakeSession()
    _execute_approved_apollo_search(
        source=source,
        client=ApolloClientAdapter(
            ApolloClient(api_key="secret", session=search_session)
        ),
        execution_request_id=search_request.id,
    )
    candidate = SDRApolloCandidate.objects.get(org=org_a, source=source)
    intent = apollo_enrichment_execution_intent(candidate)
    add_test_target(
        org=org_a,
        actor=admin_profile,
        channel=ExecutionChannel.APOLLO,
        identifier=intent.test_target_identifier,
        safe_label="Dedicated Apollo enrichment target",
    )
    approval = issue_execution_approval(
        org=org_a,
        approved_by=admin_profile,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
    )
    enrichment_request = reserve_execution(
        org=org_a,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
        approval_id=approval.id,
        idempotency_key=uuid4(),
    ).request
    candidate.enrichment_request = enrichment_request
    candidate.status = ApolloCandidateStatus.ENRICHMENT_RESERVED
    candidate.save(update_fields=["enrichment_request", "status", "updated_at"])
    mark_execution_sending(org=org_a, request_id=enrichment_request.id)

    batch = persist_apollo_enrichment_import(
        source=source,
        candidate=candidate,
        execution_request=enrichment_request,
        person={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "title": "CTO",
            "linkedin_url": "https://linkedin.com/in/ada",
            "organization": {"name": "Analytical Engines"},
            "raw_transcript": "must never be stored",
        },
    )

    batch = PersonImportBatch.objects.get(id=batch.id, org=org_a)
    record = batch.records.get()
    assert batch.source == EvidenceSource.APOLLO
    assert batch.source_namespace == "apollo:person"
    assert "raw_transcript" not in str(record.normalized_payload)
    assert batch.automation_job_id is not None
    candidate.refresh_from_db()
    assert candidate.import_batch_id == batch.id
    assert candidate.enrichment_request_id == enrichment_request.id
    assert candidate.status == ApolloCandidateStatus.IMPORT_QUEUED


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_one_approved_enrichment_calls_provider_once_and_queues_person_import(
    org_a,
    admin_profile,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    session = FakeSession(
        response=FakeResponse(
            {
                "person": {
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "email": "ada@example.com",
                    "title": "CTO",
                    "linkedin_url": "https://linkedin.com/in/ada",
                    "organization": {"name": "Analytical Engines"},
                }
            }
        )
    )
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    result = _execute_approved_apollo_enrichment(
        source=source,
        candidate=candidate,
        client=client,
        execution_request_id=request.id,
    )

    request.refresh_from_db()
    candidate.refresh_from_db()
    assert request.status == ExternalRequestStatus.DELIVERED
    assert candidate.status == ApolloCandidateStatus.IMPORT_QUEUED
    assert str(candidate.import_batch_id) == result["person_import_batch_id"]
    assert len(session.calls) == 1
    batch = PersonImportBatch.objects.get(id=candidate.import_batch_id, org=org_a)
    assert batch.source == EvidenceSource.APOLLO
    assert batch.source_namespace == "apollo:person"

    with pytest.raises(ProviderAdapterError) as replay:
        _execute_approved_apollo_enrichment(
            source=source,
            candidate=candidate,
            client=client,
            execution_request_id=request.id,
        )
    assert replay.value.error_code == "apollo_execution_not_replayable"
    assert len(session.calls) == 1


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_enrichment_transport_uncertainty_is_unknown_and_not_replayed(
    org_a,
    admin_profile,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    session = FakeSession(error=requests.Timeout("unknown provider outcome"))
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    with pytest.raises(ProviderAdapterError) as exc_info:
        _execute_approved_apollo_enrichment(
            source=source,
            candidate=candidate,
            client=client,
            execution_request_id=request.id,
        )
    assert exc_info.value.error_code == "apollo_transport_error"
    request.refresh_from_db()
    candidate.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert candidate.status == ApolloCandidateStatus.UNKNOWN
    assert len(session.calls) == 1

    with pytest.raises(ProviderAdapterError) as replay:
        _execute_approved_apollo_enrichment(
            source=source,
            candidate=candidate,
            client=client,
            execution_request_id=request.id,
        )
    assert replay.value.error_code == "apollo_execution_not_replayable"
    assert len(session.calls) == 1


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_explicit_enrichment_4xx_refunds_and_returns_candidate_to_pending(
    org_a,
    admin_profile,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    session = FakeSession(
        response=FakeResponse({"message": "not allowed"}, status_code=403)
    )
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    with pytest.raises(ProviderAdapterError) as exc_info:
        _execute_approved_apollo_enrichment(
            source=source,
            candidate=candidate,
            client=client,
            execution_request_id=request.id,
        )
    assert exc_info.value.error_code == "apollo_http_403"
    request.refresh_from_db()
    candidate.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    assert candidate.status == ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL
    # Search consumed one unit; the rejected enrichment unit was refunded.
    assert (
        ChannelExecutionControl.objects.get(
            org=org_a,
            channel=ExecutionChannel.APOLLO,
        ).consumed_units
        == 1
    )


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_enrichment_local_import_failure_is_unknown(
    org_a,
    admin_profile,
    monkeypatch,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    session = FakeSession(
        response=FakeResponse({"person": {"email": "local-failure@example.com"}})
    )
    client = ApolloClientAdapter(ApolloClient(api_key="secret", session=session))

    def fail_import(**kwargs):
        raise RuntimeError("person ledger unavailable")

    monkeypatch.setattr("sdr.sources.persist_apollo_enrichment_import", fail_import)
    with pytest.raises(RuntimeError, match="person ledger unavailable"):
        _execute_approved_apollo_enrichment(
            source=source,
            candidate=candidate,
            client=client,
            execution_request_id=request.id,
        )

    request.refresh_from_db()
    candidate.refresh_from_db()
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert candidate.status == ApolloCandidateStatus.UNKNOWN
    assert len(session.calls) == 1


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_new_search_does_not_reset_unknown_enrichment_candidate(
    org_a,
    admin_profile,
):
    source = _source(org_a)
    first_request = _reserve_search(source, admin_profile)
    _execute_approved_apollo_search(
        source=source,
        client=ApolloClientAdapter(
            ApolloClient(api_key="secret", session=FakeSession())
        ),
        execution_request_id=first_request.id,
    )
    candidate = SDRApolloCandidate.objects.get(org=org_a, source=source)
    candidate.status = ApolloCandidateStatus.UNKNOWN
    candidate.save(update_fields=["status", "updated_at"])

    second_request = _reserve_search(source, admin_profile)
    _execute_approved_apollo_search(
        source=source,
        client=ApolloClientAdapter(
            ApolloClient(api_key="secret", session=FakeSession())
        ),
        execution_request_id=second_request.id,
    )

    candidate.refresh_from_db()
    assert candidate.status == ApolloCandidateStatus.UNKNOWN
    assert candidate.search_request_id == second_request.id


@pytest.mark.django_db
@override_settings(ROOT_URLCONF="integrations.tests.urls")
def test_manual_sync_first_post_returns_exact_search_intent_without_queueing(
    admin_client,
    org_a,
):
    source = _source(org_a)

    response = admin_client.post(
        f"/api/sdr/outbound/sources/{source.id}/sync/",
        {},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approval_required"
    assert response.json()["intent"] == apollo_search_execution_intent(source).as_dict()
    assert not ExternalExecutionRequest.objects.filter(org=org_a).exists()


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
    ROOT_URLCONF="integrations.tests.urls",
)
def test_manual_sync_second_post_reserves_exact_search_and_queues_single_attempt(
    admin_client,
    org_a,
    admin_profile,
):
    connection = ApolloConnection(org=org_a, is_active=True)
    connection.set_api_key("mock-only-secret")
    connection.save()
    source = _source(org_a)
    intent = apollo_search_execution_intent(source)
    _enable_apollo(
        org_a,
        admin_profile,
        target_identifier=intent.test_target_identifier,
    )
    approval = issue_execution_approval(
        org=org_a,
        approved_by=admin_profile,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
    )
    idempotency_key = uuid4()

    response = admin_client.post(
        f"/api/sdr/outbound/sources/{source.id}/sync/",
        {
            "approval_id": str(approval.id),
            "idempotency_key": str(idempotency_key),
        },
        format="json",
    )

    assert response.status_code == 202, response.json()
    request = ExternalExecutionRequest.objects.get(
        org=org_a,
        idempotency_key=idempotency_key,
    )
    assert request.status == ExternalRequestStatus.RESERVED
    assert request.action == "search_people"
    assert request.units == 1
    job = AutomationJob.objects.get(id=response.json()["job_id"], org=org_a)
    assert job.max_attempts == 1
    assert job.payload["execution_request_id"] == str(request.id)

    replay = admin_client.post(
        f"/api/sdr/outbound/sources/{source.id}/sync/",
        {
            "approval_id": str(approval.id),
            "idempotency_key": str(idempotency_key),
        },
        format="json",
    )
    assert replay.status_code == 202, replay.json()
    assert replay.json()["job_id"] == str(job.id)
    assert (
        ExternalExecutionRequest.objects.filter(
            org=org_a,
            idempotency_key=idempotency_key,
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
    ROOT_URLCONF="integrations.tests.urls",
)
def test_candidate_enrichment_api_uses_safe_local_target_and_single_attempt_job(
    admin_client,
    org_a,
    admin_profile,
):
    connection = ApolloConnection(org=org_a, is_active=True)
    connection.set_api_key("mock-only-secret")
    connection.save()
    source = _source(org_a)
    search_request = _reserve_search(source, admin_profile)
    _execute_approved_apollo_search(
        source=source,
        client=ApolloClientAdapter(
            ApolloClient(api_key="secret", session=FakeSession())
        ),
        execution_request_id=search_request.id,
    )
    candidate = SDRApolloCandidate.objects.get(org=org_a, source=source)
    url = f"/api/sdr/outbound/apollo-candidates/{candidate.id}/enrich/"

    intent_response = admin_client.post(url, {}, format="json")

    assert intent_response.status_code == 200, intent_response.json()
    intent_data = intent_response.json()["intent"]
    assert intent_data["action"] == "enrich_person"
    assert intent_data["units"] == 1
    assert intent_data["test_target_identifier"] == (f"apollo-candidate:{candidate.id}")
    assert "apollo-person-1" not in str(intent_response.json())

    intent = apollo_enrichment_execution_intent(candidate)
    add_test_target(
        org=org_a,
        actor=admin_profile,
        channel=ExecutionChannel.APOLLO,
        identifier=intent.test_target_identifier,
        safe_label="Dedicated local Apollo candidate",
    )
    approval = issue_execution_approval(
        org=org_a,
        approved_by=admin_profile,
        channel=intent.channel,
        action=intent.action,
        target_hash=intent.target_hash,
        payload_hash=intent.payload_hash,
        units=1,
    )
    idempotency_key = uuid4()
    queued = admin_client.post(
        url,
        {
            "approval_id": str(approval.id),
            "idempotency_key": str(idempotency_key),
        },
        format="json",
    )

    assert queued.status_code == 202, queued.json()
    candidate.refresh_from_db()
    assert candidate.status == ApolloCandidateStatus.ENRICHMENT_RESERVED
    request = ExternalExecutionRequest.objects.get(
        org=org_a,
        idempotency_key=idempotency_key,
    )
    assert request.id == candidate.enrichment_request_id
    assert request.action == "enrich_person"
    assert request.units == 1
    job = AutomationJob.objects.get(id=queued.json()["job_id"], org=org_a)
    assert job.name == APOLLO_CANDIDATE_ENRICH_JOB
    assert job.max_attempts == 1
    assert job.payload["execution_request_id"] == str(request.id)

    replay = admin_client.post(
        url,
        {
            "approval_id": str(approval.id),
            "idempotency_key": str(idempotency_key),
        },
        format="json",
    )
    assert replay.status_code == 202, replay.json()
    assert replay.json()["job_id"] == str(job.id)


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_search_client_preflight_failure_releases_quota_and_persists_safe_error(
    org_a,
    admin_profile,
    monkeypatch,
):
    source = _source(org_a)
    request = _reserve_search(source, admin_profile)
    sensitive_error = "cannot decrypt apollo-person-1 must-not-leak@example.com"

    class BrokenAdapter:
        def client_for(self, *, org_id):
            del org_id
            raise RuntimeError(sensitive_error)

    monkeypatch.setattr(
        "sdr.sources.prospect_source_adapter",
        lambda provider: BrokenAdapter(),
    )
    with pytest.raises(PermanentJobError) as exc_info:
        process_outbound_source_sync_job(
            {
                "org_id": str(org_a.id),
                "source_id": str(source.id),
                "manual": True,
                "execution_request_id": str(request.id),
            }
        )

    request.refresh_from_db()
    source.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    assert sensitive_error not in str(exc_info.value)
    assert sensitive_error not in source.last_error_message


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_enrichment_client_preflight_failure_releases_and_resets_candidate(
    org_a,
    admin_profile,
    monkeypatch,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    sensitive_error = "bad key for apollo-person-1 must-not-leak@example.com"

    class BrokenAdapter:
        def client_for(self, *, org_id):
            del org_id
            raise RuntimeError(sensitive_error)

    monkeypatch.setattr(
        "sdr.sources.prospect_source_adapter",
        lambda provider: BrokenAdapter(),
    )
    with pytest.raises(PermanentJobError) as exc_info:
        process_apollo_candidate_enrichment_job(
            {
                "org_id": str(org_a.id),
                "source_id": str(source.id),
                "candidate_id": str(candidate.id),
                "execution_request_id": str(request.id),
            }
        )

    request.refresh_from_db()
    candidate.refresh_from_db()
    source.refresh_from_db()
    assert request.status == ExternalRequestStatus.FAILED
    assert candidate.status == ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL
    assert sensitive_error not in str(exc_info.value)
    assert sensitive_error not in source.last_error_message


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
    ROOT_URLCONF="integrations.tests.urls",
)
def test_other_org_cannot_discover_or_enrich_apollo_candidate(
    org_a,
    admin_profile,
    org_b,
    profile_b,
    org_b_client,
):
    del org_b, profile_b
    source = _source(org_a)
    search_request = _reserve_search(source, admin_profile)
    _execute_approved_apollo_search(
        source=source,
        client=ApolloClientAdapter(
            ApolloClient(api_key="secret", session=FakeSession())
        ),
        execution_request_id=search_request.id,
    )
    candidate = SDRApolloCandidate.objects.get(org=org_a, source=source)

    list_response = org_b_client.get(
        f"/api/sdr/outbound/sources/{source.id}/apollo-candidates/"
    )
    enrich_response = org_b_client.post(
        f"/api/sdr/outbound/apollo-candidates/{candidate.id}/enrich/",
        {},
        format="json",
    )

    assert list_response.status_code == 404
    assert enrich_response.status_code == 404


def test_enrichment_payload_hash_is_exact_and_does_not_expose_provider_id():
    assert _payload_hash({"person_id": "apollo-person-1"}) != "apollo-person-1"


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_apollo_reconciler_refunds_stale_reserved_and_marks_candidate_pending(
    org_a,
    admin_profile,
    monkeypatch,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    ExternalExecutionRequest.objects.filter(id=request.id).update(
        reserved_at=timezone.now() - timedelta(hours=1)
    )
    monkeypatch.setattr(
        "sdr.sources.prospect_source_adapter",
        lambda provider: pytest.fail(f"provider adapter was called for {provider}"),
    )

    result = reconcile_apollo_candidate_states(
        org=org_a,
        reserved_before=timezone.now() - timedelta(minutes=30),
        sending_before=timezone.now() - timedelta(minutes=30),
    )

    request.refresh_from_db()
    candidate.refresh_from_db()
    assert result["released_requests"] == 1
    assert result["unknown_requests"] == 0
    assert request.status == ExternalRequestStatus.FAILED
    assert candidate.status == ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_apollo_reconciler_never_replays_stale_sending_and_marks_unknown(
    org_a,
    admin_profile,
    monkeypatch,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    mark_execution_sending(org=org_a, request_id=request.id)
    ExternalExecutionRequest.objects.filter(id=request.id).update(
        sending_at=timezone.now() - timedelta(hours=1)
    )
    monkeypatch.setattr(
        "sdr.sources.prospect_source_adapter",
        lambda provider: pytest.fail(f"provider adapter was called for {provider}"),
    )

    result = reconcile_apollo_candidate_states(
        org=org_a,
        reserved_before=timezone.now() - timedelta(minutes=30),
        sending_before=timezone.now() - timedelta(minutes=30),
    )

    request.refresh_from_db()
    candidate.refresh_from_db()
    assert result["released_requests"] == 0
    assert result["unknown_requests"] == 1
    assert request.status == ExternalRequestStatus.UNKNOWN
    assert candidate.status == ApolloCandidateStatus.UNKNOWN


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
@pytest.mark.parametrize(
    ("outcome", "expected_request_status", "expected_candidate_status"),
    [
        (
            "delivered",
            ExternalRequestStatus.DELIVERED,
            ApolloCandidateStatus.SKIPPED,
        ),
        (
            "failed_consumed",
            ExternalRequestStatus.FAILED,
            ApolloCandidateStatus.PENDING_ENRICHMENT_APPROVAL,
        ),
    ],
)
def test_manual_unknown_resolution_updates_apollo_candidate_without_replay(
    org_a,
    admin_profile,
    outcome,
    expected_request_status,
    expected_candidate_status,
):
    source = _source(org_a)
    candidate, request = _candidate_with_reserved_enrichment(source, admin_profile)
    mark_execution_sending(org=org_a, request_id=request.id)
    mark_provider_accepted(
        org=org_a,
        request_id=request.id,
        local_state_uncertain=True,
    )
    SDRApolloCandidate.objects.filter(id=candidate.id).update(
        status=ApolloCandidateStatus.UNKNOWN
    )

    resolve_unknown_execution(
        org=org_a,
        actor=admin_profile,
        request_id=request.id,
        outcome=outcome,
    )

    request.refresh_from_db()
    candidate.refresh_from_db()
    assert request.status == expected_request_status
    assert candidate.status == expected_candidate_status


@pytest.mark.django_db
@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
@pytest.mark.parametrize(
    ("batch_status", "job_status", "expected_candidate_status", "counter"),
    [
        (
            PersonImportBatchStatus.COMPLETED,
            None,
            ApolloCandidateStatus.IMPORTED,
            "candidates_imported",
        ),
        (
            PersonImportBatchStatus.PARTIAL,
            None,
            ApolloCandidateStatus.IMPORT_REVIEW_REQUIRED,
            "candidates_import_review_required",
        ),
        (
            PersonImportBatchStatus.FAILED,
            None,
            ApolloCandidateStatus.IMPORT_FAILED,
            "candidates_import_failed",
        ),
        (
            PersonImportBatchStatus.RUNNING,
            AutomationJobStatus.DEAD_LETTER,
            ApolloCandidateStatus.IMPORT_RETRY_REQUIRED,
            "candidates_import_retry_required",
        ),
    ],
)
def test_apollo_import_batch_terminal_state_is_projected_to_candidate(
    org_a,
    admin_profile,
    batch_status,
    job_status,
    expected_candidate_status,
    counter,
):
    source = _source(org_a)
    candidate, _request = _candidate_with_reserved_enrichment(source, admin_profile)
    job = None
    if job_status is not None:
        job = AutomationJob.objects.create(
            org=org_a,
            name="matching.import_people",
            idempotency_key=f"apollo-import-terminal:{uuid4()}",
            payload={},
            status=job_status,
            scheduled_for=timezone.now(),
        )
    batch = PersonImportBatch.objects.create(
        org=org_a,
        idempotency_key=uuid4(),
        request_hash="a" * 64,
        content_hash="b" * 64,
        original_filename="apollo-provider-import.json",
        source="apollo",
        source_namespace="apollo:person",
        status=batch_status,
        automation_job=job,
    )
    SDRApolloCandidate.objects.filter(id=candidate.id).update(
        import_batch=batch,
        status=ApolloCandidateStatus.IMPORT_QUEUED,
    )

    result = reconcile_apollo_candidate_states(
        org=org_a,
        reserved_before=timezone.now() - timedelta(days=1),
        sending_before=timezone.now() - timedelta(days=1),
    )

    candidate.refresh_from_db()
    assert candidate.status == expected_candidate_status
    assert result[counter] == 1
