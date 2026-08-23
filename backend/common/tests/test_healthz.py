import pytest
from django.test import override_settings
from django.urls import re_path
from django.views.generic import TemplateView

urlpatterns = [
    re_path(
        r"^healthz/$",
        TemplateView.as_view(template_name="healthz.html"),
        name="healthz",
    )
]


@pytest.mark.django_db
@override_settings(ROOT_URLCONF=__name__)
def test_healthz_does_not_require_organization_context(client):
    response = client.get("/healthz/")

    assert response.status_code == 200
