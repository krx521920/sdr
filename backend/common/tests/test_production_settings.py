import base64
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_INTEGRATION_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")


def _production_env(tmp_path):
    env = os.environ.copy()
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_BUCKET_NAME",
        "AWS_SECRET_ACCESS_KEY",
        "INTEGRATION_ENCRYPTION_KEY",
        "SENTRY_DSN",
    ):
        env.pop(name, None)
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "crm.settings",
            "ENV_TYPE": "production",
            "SECRET_KEY": "s" * 64,
            "INTEGRATION_ENCRYPTION_KEY": TEST_INTEGRATION_ENCRYPTION_KEY,
            "EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend",
            "SERVER_LOG_PATH": str(tmp_path / "server.log"),
            "SECURITY_AUDIT_LOG_PATH": str(tmp_path / "security.log"),
            "STATIC_ROOT": str(tmp_path / "static"),
        }
    )
    return env


def test_production_settings_load_without_optional_cloud_services(tmp_path):
    env = _production_env(tmp_path)
    env["USE_S3_STORAGE"] = "false"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from django.conf import settings; "
                "print(json.dumps({"
                "'production': settings.IS_PRODUCTION, "
                "'debug': settings.DEBUG, "
                "'email_backend': settings.EMAIL_BACKEND, "
                "'aws_enabled': settings.AWS_ENABLED, "
                "'sentry_enabled': bool(settings.SENTRY_DSN), "
                "'integration_encryption_configured': "
                "bool(settings.INTEGRATION_ENCRYPTION_KEY)"
                "}))"
            ),
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "production": True,
        "debug": False,
        "email_backend": "django.core.mail.backends.console.EmailBackend",
        "aws_enabled": False,
        "sentry_enabled": False,
        "integration_encryption_configured": True,
    }


def test_production_settings_require_bucket_when_s3_explicitly_enabled(tmp_path):
    env = _production_env(tmp_path)
    env["USE_S3_STORAGE"] = "true"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.DEBUG)",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "AWS_BUCKET_NAME is required" in result.stderr


def test_production_settings_require_integration_encryption_key(tmp_path):
    env = _production_env(tmp_path)
    env.pop("INTEGRATION_ENCRYPTION_KEY")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.DEBUG)",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "INTEGRATION_ENCRYPTION_KEY is required in production" in result.stderr


def test_production_settings_reject_invalid_integration_encryption_key(tmp_path):
    env = _production_env(tmp_path)
    invalid_key = "not-a-fernet-key"
    env["INTEGRATION_ENCRYPTION_KEY"] = invalid_key
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.DEBUG)",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "INTEGRATION_ENCRYPTION_KEY must be a valid Fernet key" in output
    assert invalid_key not in output


def test_settings_reject_invalid_explicit_ai_provider_allowlist(tmp_path):
    env = _production_env(tmp_path)
    env["AI_GATEWAY_ALLOWED_PROVIDERS"] = "unknown-provider"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.DEBUG)",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "AI_GATEWAY_ALLOWED_PROVIDERS must contain" in result.stderr
