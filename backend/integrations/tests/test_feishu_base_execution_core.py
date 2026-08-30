from types import SimpleNamespace
from uuid import uuid4

import pytest

from integrations.models import FeishuBaseSync
from integrations.providers.feishu_base import client as client_module
from integrations.providers.feishu_base.client import (
    FEISHU_DELETE_RESEARCH_ACTION,
    FEISHU_SYNC_RESEARCH_ACTION,
    FEISHU_VALIDATE_SCHEMA_ACTION,
    FeishuBaseAPIError,
    FeishuBaseClient,
    FeishuBaseConfigurationError,
    feishu_base_target_identifier,
)


class _Response:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected provider request")
        return self.responses.pop(0)


@pytest.fixture
def allow_bound_provider_io(monkeypatch):
    calls = []

    def authorize(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(client_module, "assert_provider_io_authorized", authorize)
    return calls


def _bound(session, *, action):
    return FeishuBaseClient(session=session).for_execution(
        org=SimpleNamespace(id=uuid4()),
        action=action,
        execution_request_id=uuid4(),
    )


def test_target_identifier_is_internal_and_connection_scoped():
    connection_id = uuid4()
    assert (
        feishu_base_target_identifier(connection_id) == f"feishu-base:{connection_id}"
    )


def test_sync_client_allows_reads_and_exactly_one_mutation(allow_bound_provider_io):
    session = _Session(
        [
            _Response({"tenant_access_token": "token", "code": 0}),
            _Response({"code": 0, "data": {"items": []}}),
            _Response({"code": 0, "data": {"items": []}}),
            _Response(
                {
                    "code": 0,
                    "data": {"record": {"record_id": "rec-created", "fields": {}}},
                }
            ),
        ]
    )
    client = _bound(session, action=FEISHU_SYNC_RESEARCH_ACTION)

    token = client.tenant_access_token(app_id="app", app_secret="secret")
    assert (
        client.list_fields(access_token=token, app_token="base", table_id="table") == []
    )
    assert (
        client.find_record_by_field(
            access_token=token,
            app_token="base",
            table_id="table",
            field_name="Intake ID",
            value="lead-1",
        )
        is None
    )
    assert (
        client.create_record(
            access_token=token,
            app_token="base",
            table_id="table",
            fields={"Intake ID": "lead-1"},
        ).record_id
        == "rec-created"
    )

    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.update_record(
            access_token=token,
            app_token="base",
            table_id="table",
            record_id="rec-created",
            fields={"Intake ID": "lead-1"},
        )
    assert exc_info.value.error_code == "feishu_execution_request_reused"
    assert len(session.calls) == 4
    assert {call["action"] for call in allow_bound_provider_io} == {
        FEISHU_SYNC_RESEARCH_ACTION
    }


def test_action_mismatch_fails_before_network(allow_bound_provider_io):
    session = _Session([])
    client = _bound(session, action=FEISHU_VALIDATE_SCHEMA_ACTION)
    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.create_record(
            access_token="token",
            app_token="base",
            table_id="table",
            fields={},
        )
    assert exc_info.value.error_code == "feishu_execution_action_mismatch"
    assert session.calls == []
    assert allow_bound_provider_io == []


def test_delete_client_burns_mutation_before_second_attempt(allow_bound_provider_io):
    session = _Session([_Response({"code": 0, "data": {}})])
    client = _bound(session, action=FEISHU_DELETE_RESEARCH_ACTION)
    client.delete_record(
        access_token="token",
        app_token="base",
        table_id="table",
        record_id="rec-1",
    )
    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.delete_record(
            access_token="token",
            app_token="base",
            table_id="table",
            record_id="rec-1",
        )
    assert exc_info.value.error_code == "feishu_execution_request_reused"
    assert len(session.calls) == 1


def test_schema_pagination_is_bounded(allow_bound_provider_io):
    session = _Session(
        [
            _Response(
                {
                    "code": 0,
                    "data": {
                        "items": [],
                        "has_more": True,
                        "page_token": f"page-{index}",
                    },
                }
            )
            for index in range(10)
        ]
    )
    client = _bound(session, action=FEISHU_VALIDATE_SCHEMA_ACTION)
    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.list_fields(access_token="token", app_token="base", table_id="table")
    assert exc_info.value.error_code == "feishu_field_page_limit_exceeded"
    assert len(session.calls) == 10


def test_schema_field_count_is_bounded(allow_bound_provider_io):
    session = _Session(
        [
            _Response(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {"field_name": f"field-{index}", "type": 1}
                            for index in range(1001)
                        ]
                    },
                }
            )
        ]
    )
    client = _bound(session, action=FEISHU_VALIDATE_SCHEMA_ACTION)
    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.list_fields(access_token="token", app_token="base", table_id="table")
    assert exc_info.value.error_code == "feishu_field_limit_exceeded"
    assert len(session.calls) == 1


def test_bound_client_read_requests_are_bounded(allow_bound_provider_io):
    session = _Session(
        [_Response({"tenant_access_token": "token", "code": 0}) for _ in range(20)]
    )
    client = _bound(session, action=FEISHU_VALIDATE_SCHEMA_ACTION)
    for _ in range(20):
        assert client.tenant_access_token(app_id="app", app_secret="secret") == "token"
    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.tenant_access_token(app_id="app", app_secret="secret")
    assert exc_info.value.error_code == "feishu_read_limit_exceeded"
    assert len(session.calls) == 20


def test_provider_error_body_is_not_promoted_to_exception(allow_bound_provider_io):
    secret_body = "private@example.com rec-sensitive"
    session = _Session(
        [_Response({"code": 1254001, "msg": secret_body}, status_code=400)]
    )
    client = _bound(session, action=FEISHU_VALIDATE_SCHEMA_ACTION)
    with pytest.raises(FeishuBaseAPIError) as exc_info:
        client.tenant_access_token(app_id="app", app_secret="secret")
    assert (
        str(exc_info.value) == "Feishu OpenAPI request did not complete successfully."
    )
    assert secret_body not in str(exc_info.value)


@pytest.mark.parametrize("status_code", [307, 308])
@pytest.mark.parametrize("operation", ["authenticate", "create", "delete"])
def test_redirects_are_never_followed_or_replayed(
    allow_bound_provider_io,
    status_code,
    operation,
):
    session = _Session([_Response({"location": "https://attacker.invalid"}, status_code=status_code)])
    action = {
        "authenticate": FEISHU_VALIDATE_SCHEMA_ACTION,
        "create": FEISHU_SYNC_RESEARCH_ACTION,
        "delete": FEISHU_DELETE_RESEARCH_ACTION,
    }[operation]
    client = _bound(session, action=action)

    with pytest.raises(FeishuBaseAPIError) as exc_info:
        if operation == "authenticate":
            client.tenant_access_token(app_id="app", app_secret="secret")
        elif operation == "create":
            client.create_record(
                access_token="token",
                app_token="base",
                table_id="table",
                fields={"Intake ID": "lead-1"},
            )
        else:
            client.delete_record(
                access_token="token",
                app_token="base",
                table_id="table",
                record_id="rec-1",
            )

    assert exc_info.value.error_code == "feishu_http_redirect"
    assert exc_info.value.status_code == status_code
    assert exc_info.value.retryable is False
    assert exc_info.value.mutation_attempted is (operation in {"create", "delete"})
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False


@pytest.mark.parametrize(
    "base_url",
    [
        "http://open.feishu.cn",
        "https://attacker.invalid",
        "https://user:password@open.feishu.cn",
        "https://open.feishu.cn:444",
        "https://open.feishu.cn?next=attacker",
        "https://open.feishu.cn#fragment",
        "https://open.feishu.cn/unapproved-prefix",
    ],
)
def test_client_rejects_unapproved_base_urls(base_url):
    with pytest.raises(FeishuBaseConfigurationError):
        FeishuBaseClient(base_url=base_url, session=_Session([]))


@pytest.mark.django_db
def test_remote_record_identifier_is_encrypted_hashed_and_clearable(org_a):
    sync = FeishuBaseSync(org=org_a)
    sync.set_record_id("rec-sensitive-provider-id")

    assert sync.get_record_id() == "rec-sensitive-provider-id"
    assert "rec-sensitive-provider-id" not in sync.record_id_ciphertext
    assert len(sync.record_id_hash) == 64
    assert sync.record_safe_label == f"Feishu Base record {sync.record_id_hash[:8]}"
    assert "rec-sensitive-provider-id" not in sync.record_safe_label
    assert sync.has_remote_record is True

    sync.clear_record_id()
    assert sync.get_record_id() == ""
    assert sync.record_id_hash == ""
    assert sync.record_safe_label == ""
    assert sync.has_remote_record is False
