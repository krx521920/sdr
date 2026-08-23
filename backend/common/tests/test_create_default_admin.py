import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError


pytestmark = pytest.mark.django_db

User = get_user_model()


def test_production_skips_default_admin_without_explicit_opt_in(monkeypatch, capsys):
    monkeypatch.setenv("ENV_TYPE", "production")
    monkeypatch.delenv("CREATE_DEFAULT_ADMIN", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")

    call_command("create_default_admin")

    assert not User.objects.filter(is_superuser=True).exists()
    assert "bootstrap is disabled" in capsys.readouterr().out


def test_production_rejects_weak_bootstrap_password(monkeypatch):
    monkeypatch.setenv("ENV_TYPE", "prod")
    monkeypatch.setenv("CREATE_DEFAULT_ADMIN", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")

    with pytest.raises(CommandError, match="at least 12 characters"):
        call_command("create_default_admin")


def test_production_allows_explicit_bootstrap_with_strong_password(monkeypatch):
    monkeypatch.setenv("ENV_TYPE", "production")
    monkeypatch.setenv("CREATE_DEFAULT_ADMIN", "true")
    monkeypatch.setenv("ADMIN_EMAIL", "bootstrap@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-bootstrap-password")

    call_command("create_default_admin")

    user = User.objects.get(email="bootstrap@example.com")
    assert user.is_superuser
    assert user.check_password("strong-bootstrap-password")


def test_development_keeps_default_bootstrap_behavior(monkeypatch):
    monkeypatch.setenv("ENV_TYPE", "dev")
    monkeypatch.delenv("CREATE_DEFAULT_ADMIN", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", "dev-admin@example.com")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    call_command("create_default_admin")

    user = User.objects.get(email="dev-admin@example.com")
    assert user.is_superuser
    assert user.check_password("admin")
