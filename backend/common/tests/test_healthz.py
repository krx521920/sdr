import pytest
from django.test import override_settings
from django.urls import path, re_path
from django.views.generic import TemplateView

from common.views.health_views import readyz

urlpatterns = [
    re_path(
        r"^healthz/$",
        TemplateView.as_view(template_name="healthz.html"),
        name="healthz",
    ),
    path("readyz/", readyz, name="readyz"),
]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_healthz_does_not_require_organization_context(client):
    response = client.get("/healthz/")

    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(
    ROOT_URLCONF=__name__,
    CELERY_BROKER_URL="redis://broker.example.invalid:6379/0",
)
def test_readyz_succeeds_without_organization_context(client, monkeypatch):
    monkeypatch.setattr("common.views.health_views._database_is_ready", lambda: True)
    monkeypatch.setattr("common.views.health_views._broker_is_ready", lambda: True)

    response = client.get("/readyz/")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "broker": "ok"},
    }
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_readyz_returns_503_when_database_fails(client, monkeypatch):
    def database_failure():
        raise RuntimeError("database connection details must stay private")

    monkeypatch.setattr(
        "common.views.health_views._database_is_ready", database_failure
    )
    monkeypatch.setattr("common.views.health_views._broker_is_ready", lambda: True)

    response = client.get("/readyz/")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "failed", "broker": "ok"},
    }
    assert b"connection details" not in response.content


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_readyz_returns_503_when_redis_fails(client, monkeypatch):
    def redis_failure():
        raise RuntimeError("redis connection details must stay private")

    monkeypatch.setattr("common.views.health_views._database_is_ready", lambda: True)
    monkeypatch.setattr("common.views.health_views._broker_is_ready", redis_failure)

    response = client.get("/readyz/")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "broker": "failed"},
    }
    assert b"connection details" not in response.content
