import json
import os
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _production_env(tmp_path):
    env = os.environ.copy()
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_BUCKET_NAME",
        "AWS_SECRET_ACCESS_KEY",
        "SENTRY_DSN",
    ):
        env.pop(name, None)
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "crm.settings",
            "ENV_TYPE": "production",
            "SECRET_KEY": "s" * 64,
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
                "'sentry_enabled': bool(settings.SENTRY_DSN)"
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
    }


def test_production_settings_require_bucket_when_s3_explicitly_enabled(tmp_path):
    env = _production_env(tmp_path)
    env["USE_S3_STORAGE"] = "true"
    result = subprocess.run(
        [sys.executable, "-c", "from django.conf import settings; print(settings.DEBUG)"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "AWS_BUCKET_NAME is required" in result.stderr
