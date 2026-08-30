"""Small client for Apollo People Search and People Enrichment."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests

from integrations.execution_safety import (
    ExecutionSafetyError,
    assert_provider_io_authorized,
)

FILTER_PARAM_MAP = {
    "person_titles": "person_titles[]",
    "person_seniorities": "person_seniorities[]",
    "person_locations": "person_locations[]",
    "organization_locations": "organization_locations[]",
    "organization_domains": "q_organization_domains_list[]",
    "employee_ranges": "organization_num_employees_ranges[]",
    "email_statuses": "contact_email_status[]",
    "technologies_any": "currently_using_any_of_technology_uids[]",
    "technologies_all": "currently_using_all_of_technology_uids[]",
    "keywords": "q_keywords",
}

APOLLO_SEARCH_ACTION = "search_people"
APOLLO_ENRICH_ACTION = "enrich_person"


class ApolloAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        error_code: str = "apollo_provider_error",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.error_code = error_code


class ApolloClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.apollo.io/api/v1",
        timeout: float = 15.0,
        session: requests.Session | None = None,
        org=None,
        execution_request_id=None,
        execution_action: str = "",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.org = org
        self.execution_request_id = execution_request_id
        self.execution_action = execution_action
        self._request_started = False

    def for_execution(self, *, org, action: str, execution_request_id):
        """Return a one-call client bound to one durable execution request."""

        return ApolloClient(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            session=self.session,
            org=org,
            execution_request_id=execution_request_id,
            execution_action=action,
        )

    def search_people(
        self,
        *,
        filters: Mapping[str, Any],
        page: int,
        per_page: int,
    ) -> Mapping[str, Any]:
        params = {
            FILTER_PARAM_MAP[key]: value
            for key, value in filters.items()
            if key in FILTER_PARAM_MAP and value not in (None, "", [])
        }
        params.update(page=page, per_page=per_page)
        payload = self._request(
            "POST",
            "mixed_people/api_search",
            params=params,
            action=APOLLO_SEARCH_ACTION,
        )
        people = payload.get("people")
        if not isinstance(people, list):
            raise ApolloAPIError(
                "Apollo returned an invalid people search response",
                retryable=False,
                error_code="apollo_invalid_search_response",
            )
        return payload

    def enrich_person(self, *, person_id: str) -> Mapping[str, Any] | None:
        payload = self._request(
            "POST",
            "people/match",
            params={"id": person_id},
            action=APOLLO_ENRICH_ACTION,
        )
        person = payload.get("person")
        if person is None:
            return None
        if not isinstance(person, Mapping):
            raise ApolloAPIError(
                "Apollo returned an invalid person enrichment response",
                retryable=False,
                error_code="apollo_invalid_enrichment_response",
            )
        return person

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any],
        action: str,
    ) -> Mapping[str, Any]:
        if self._request_started:
            raise ApolloAPIError(
                "An Apollo execution request can authorize only one provider call",
                retryable=False,
                error_code="apollo_execution_request_reused",
            )
        if self.execution_action and self.execution_action != action:
            raise ApolloAPIError(
                "Apollo execution action does not match the provider call",
                retryable=False,
                error_code="apollo_execution_action_mismatch",
            )
        try:
            assert_provider_io_authorized(
                org=self.org,
                channel="apollo",
                action=action,
                execution_request_id=self.execution_request_id,
            )
        except ExecutionSafetyError as exc:
            raise ApolloAPIError(
                exc.detail, retryable=False, error_code=exc.code
            ) from exc
        # Burn this in-memory capability before entering requests. A durable
        # replay is also rejected by the orchestration layer.
        self._request_started = True
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/{path}",
                headers={
                    "accept": "application/json",
                    "Cache-Control": "no-cache",
                    "x-api-key": self.api_key,
                },
                params=dict(params),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApolloAPIError(
                "Apollo request failed",
                retryable=True,
                error_code="apollo_transport_error",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApolloAPIError(
                "Apollo returned a non-JSON response",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
                error_code="apollo_invalid_response",
            ) from exc
        if response.status_code >= 400 or not isinstance(payload, Mapping):
            # Provider response bodies are untrusted and can echo person ids,
            # emails, or query values.  Never promote that text into an
            # exception: automation persists exception messages.
            raise ApolloAPIError(
                "Apollo rejected the request",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
                error_code=f"apollo_http_{response.status_code}",
            )
        return payload
