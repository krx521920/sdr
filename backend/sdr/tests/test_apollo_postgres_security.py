from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from threading import Barrier, Event, Lock
from uuid import uuid4

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import override_settings
from django.utils import timezone

from automation.tenant_context import database_org_context
from integrations.execution_safety import (
    add_test_target,
    configure_channel,
    configure_organization_execution,
    issue_execution_approval,
    reserve_execution,
)
from integrations.models import (
    ChannelExecutionApproval,
    ExecutionChannel,
    ExternalExecutionRequest,
)
from matching.models import PersonImportBatch
from sdr.models import SDRApolloCandidate, SDROutboundCampaign, SDROutboundSource
from sdr.provider_ports import ProviderAdapterError
from sdr.sources import (
    _execute_approved_apollo_search,
    apollo_search_execution_intent,
)

pytestmark = [
    pytest.mark.postgres_only,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL RLS and Apollo candidate triggers are required.",
    ),
    pytest.mark.django_db(transaction=True),
]


SOURCE_ORG_MESSAGE = "Apollo candidate source organization mismatch"
SEARCH_REQUEST_MESSAGE = "Apollo candidate search request mismatch"
ENRICHMENT_REQUEST_MESSAGE = "Apollo candidate enrichment request mismatch"
IMPORT_BATCH_ORG_MESSAGE = "Apollo candidate import batch organization mismatch"
CHECK_VIOLATION_SQLSTATE = "23514"


@contextmanager
def _empty_database_org_context():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_org', true)")
        previous = cursor.fetchone()[0] or ""
        cursor.execute("SELECT set_config('app.current_org', '', false)")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_org', %s, false)",
                [previous],
            )


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _make_execution_request(*, org, profile, channel, action, suffix):
    approval = ChannelExecutionApproval.objects.create(
        org=org,
        channel=channel,
        idempotency_key=uuid4(),
        request_hash=_digest(f"approval-request:{suffix}"),
        action=action,
        target_hash=_digest(f"target:{suffix}"),
        payload_hash=_digest(f"payload:{suffix}"),
        units=1,
        approved_by=profile,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return ExternalExecutionRequest.objects.create(
        org=org,
        channel=channel,
        action=action,
        idempotency_key=uuid4(),
        request_hash=_digest(f"execution-request:{suffix}"),
        target_hash=approval.target_hash,
        payload_hash=approval.payload_hash,
        units=1,
        approval=approval,
        reserved_at=timezone.now(),
    )


def _make_graph(*, org, profile, suffix):
    campaign = SDROutboundCampaign.objects.create(
        org=org,
        owner=profile,
        name=f"Apollo PostgreSQL campaign {suffix}",
    )
    source = SDROutboundSource.objects.create(
        org=org,
        campaign=campaign,
        name=f"Apollo PostgreSQL source {suffix}",
        provider="apollo",
    )
    search_request = _make_execution_request(
        org=org,
        profile=profile,
        channel=ExecutionChannel.APOLLO,
        action="search_people",
        suffix=f"search:{suffix}",
    )
    enrichment_request = _make_execution_request(
        org=org,
        profile=profile,
        channel=ExecutionChannel.APOLLO,
        action="enrich_person",
        suffix=f"enrichment:{suffix}",
    )
    import_batch = PersonImportBatch.objects.create(
        org=org,
        requested_by=profile,
        idempotency_key=uuid4(),
        request_hash=_digest(f"import-request:{suffix}"),
        content_hash=_digest(f"import-content:{suffix}"),
        original_filename=f"apollo-{suffix}.csv",
        source="apollo",
        source_namespace="apollo:people",
    )
    candidate = SDRApolloCandidate.objects.create(
        org=org,
        source=source,
        search_request=search_request,
        enrichment_request=enrichment_request,
        import_batch=import_batch,
        provider_person_id_ciphertext=f"encrypted-apollo-id:{suffix}",
        provider_person_id_hash=_digest(f"apollo-person:{suffix}"),
        safe_label=f"Apollo candidate {suffix}",
    )
    return {
        "source": source,
        "search_request": search_request,
        "enrichment_request": enrichment_request,
        "import_batch": import_batch,
        "candidate": candidate,
    }


def _raw_update(instance, column, value):
    quote = connection.ops.quote_name
    table = quote(instance._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET {quote(column)} = %s "
            f"WHERE {quote('id')} = %s",
            [value, instance.id],
        )


def _sqlstate(error):
    current = error
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        diagnostic = getattr(current, "diag", None)
        state = getattr(diagnostic, "sqlstate", None)
        if state:
            return state
        current = getattr(current, "__cause__", None) or getattr(
            current,
            "__context__",
            None,
        )
    return None


def _assert_raw_update_rejected(instance, column, value, message):
    with pytest.raises(DatabaseError) as exc_info:
        with transaction.atomic():
            _raw_update(instance, column, value)

    assert message in str(exc_info.value)
    assert _sqlstate(exc_info.value) == CHECK_VIOLATION_SQLSTATE


def test_apollo_candidate_is_isolated_by_postgres_rls(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
    profile_b,
):
    with database_org_context(org_a.id):
        own = _make_graph(org=org_a, profile=admin_profile, suffix="rls-a")
    with database_org_context(org_b.id):
        other = _make_graph(org=org_b, profile=profile_b, suffix="rls-b")

    with database_org_context(org_a.id):
        assert list(SDRApolloCandidate.objects.values_list("id", flat=True)) == [
            own["candidate"].id
        ]
        assert not SDRApolloCandidate.objects.filter(
            id=other["candidate"].id
        ).exists()

    with database_org_context(org_b.id):
        assert list(SDRApolloCandidate.objects.values_list("id", flat=True)) == [
            other["candidate"].id
        ]
        assert not SDRApolloCandidate.objects.filter(
            id=own["candidate"].id
        ).exists()

    with _empty_database_org_context():
        assert SDRApolloCandidate.objects.count() == 0


def test_apollo_candidate_trigger_rejects_cross_org_raw_updates(
    transactional_db,
    org_a,
    org_b,
    admin_profile,
    profile_b,
):
    with database_org_context(org_a.id):
        own = _make_graph(org=org_a, profile=admin_profile, suffix="guard-a")
    with database_org_context(org_b.id):
        other = _make_graph(org=org_b, profile=profile_b, suffix="guard-b")

    cases = (
        ("source_id", other["source"].id, SOURCE_ORG_MESSAGE),
        (
            "search_request_id",
            other["search_request"].id,
            SEARCH_REQUEST_MESSAGE,
        ),
        (
            "enrichment_request_id",
            other["enrichment_request"].id,
            ENRICHMENT_REQUEST_MESSAGE,
        ),
        (
            "import_batch_id",
            other["import_batch"].id,
            IMPORT_BATCH_ORG_MESSAGE,
        ),
        ("org_id", org_b.id, SOURCE_ORG_MESSAGE),
    )

    with database_org_context(org_a.id):
        for column, value, message in cases:
            _assert_raw_update_rejected(
                own["candidate"],
                column,
                value,
                message,
            )


def test_apollo_candidate_trigger_binds_search_and_enrichment_request_contracts(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        graph = _make_graph(org=org_a, profile=admin_profile, suffix="binding")
        invalid_search_action = _make_execution_request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.APOLLO,
            action="enrich_person",
            suffix="invalid-search-action",
        )
        invalid_search_channel = _make_execution_request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.EMAIL,
            action="search_people",
            suffix="invalid-search-channel",
        )
        invalid_enrichment_action = _make_execution_request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.APOLLO,
            action="search_people",
            suffix="invalid-enrichment-action",
        )
        invalid_enrichment_channel = _make_execution_request(
            org=org_a,
            profile=admin_profile,
            channel=ExecutionChannel.EMAIL,
            action="enrich_person",
            suffix="invalid-enrichment-channel",
        )

        for request in (invalid_search_action, invalid_search_channel):
            _assert_raw_update_rejected(
                graph["candidate"],
                "search_request_id",
                request.id,
                SEARCH_REQUEST_MESSAGE,
            )

        for request in (invalid_enrichment_action, invalid_enrichment_channel):
            _assert_raw_update_rejected(
                graph["candidate"],
                "enrichment_request_id",
                request.id,
                ENRICHMENT_REQUEST_MESSAGE,
            )


class _ConcurrentApolloClient:
    def __init__(self):
        self.call_count = 0
        self.lock = Lock()
        self.started = Event()
        self.release = Event()

    def for_execution(self, **kwargs):
        del kwargs
        return self

    def search_people(self, **kwargs):
        del kwargs
        with self.lock:
            self.call_count += 1
        self.started.set()
        assert self.release.wait(timeout=10), "concurrent Apollo test was not released"
        return {"people": [], "pagination": {"total_entries": 0}}


@override_settings(
    ALLOW_UNGUARDED_PROVIDER_IO=False,
    REAL_CHANNEL_EXECUTION_ENABLED=True,
)
def test_two_postgres_workers_claim_one_apollo_request_for_one_provider_call(
    transactional_db,
    org_a,
    admin_profile,
):
    with database_org_context(org_a.id):
        campaign = SDROutboundCampaign.objects.create(
            org=org_a,
            owner=admin_profile,
            name="Concurrent Apollo claim",
        )
        source = SDROutboundSource.objects.create(
            org=org_a,
            campaign=campaign,
            name="Concurrent Apollo source",
            provider="apollo",
            search_filters={"person_titles": ["CTO"]},
            max_results_per_sync=1,
            enrichment_credits_acknowledged=True,
        )
        intent = apollo_search_execution_intent(source)
        configure_organization_execution(
            org=org_a,
            actor=admin_profile,
            enabled=True,
            daily_limit=10,
        )
        configure_channel(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.APOLLO,
            enabled=True,
            test_mode=True,
            daily_limit=10,
            per_execution_limit=1,
        )
        add_test_target(
            org=org_a,
            actor=admin_profile,
            channel=ExecutionChannel.APOLLO,
            identifier=intent.test_target_identifier,
            safe_label="Concurrent Apollo target",
        )
        approval = issue_execution_approval(
            org=org_a,
            approved_by=admin_profile,
            channel=intent.channel,
            action=intent.action,
            target_hash=intent.target_hash,
            payload_hash=intent.payload_hash,
            units=1,
        ).approval
        request = reserve_execution(
            org=org_a,
            channel=intent.channel,
            action=intent.action,
            target_hash=intent.target_hash,
            payload_hash=intent.payload_hash,
            units=1,
            approval_id=approval.id,
            idempotency_key=uuid4(),
        ).request

    client = _ConcurrentApolloClient()
    start = Barrier(2)

    def worker():
        close_old_connections()
        try:
            with database_org_context(org_a.id):
                local_source = SDROutboundSource.objects.select_related(
                    "org", "campaign"
                ).get(id=source.id, org_id=org_a.id)
                start.wait(timeout=10)
                try:
                    _execute_approved_apollo_search(
                        source=local_source,
                        client=client,
                        execution_request_id=request.id,
                    )
                except ProviderAdapterError as exc:
                    return exc.error_code
                return "succeeded"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        assert client.started.wait(timeout=10), "no worker reached the provider boundary"
        done, _pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
        assert done, "the competing worker did not observe the claimed request"
        client.release.set()
        results = [future.result(timeout=10) for future in futures]

    assert client.call_count == 1
    assert sorted(results) == ["apollo_execution_not_replayable", "succeeded"]
    with database_org_context(org_a.id):
        request.refresh_from_db()
        assert request.status == "delivered"
