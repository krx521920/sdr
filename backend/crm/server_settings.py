import os
from pathlib import Path

import sentry_sdk
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.django import DjangoIntegration


BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = False


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# S3 is optional. This supports single-host/container deployments that persist
# MEDIA_ROOT locally, while allowing cloud deployments to opt into S3.
USE_S3_STORAGE = _env_bool(
    "USE_S3_STORAGE", default=bool(os.environ.get("AWS_BUCKET_NAME"))
)
if USE_S3_STORAGE:
    AWS_STORAGE_BUCKET_NAME = AWS_BUCKET_NAME = os.environ.get("AWS_BUCKET_NAME", "")
    if not AWS_BUCKET_NAME:
        raise ImproperlyConfigured(
            "AWS_BUCKET_NAME is required when USE_S3_STORAGE=true."
        )

    # Explicit keys are optional so production can use an IAM task/instance role.
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    S3_DOMAIN = AWS_S3_CUSTOM_DOMAIN = os.environ.get(
        "AWS_S3_CUSTOM_DOMAIN", f"{AWS_BUCKET_NAME}.s3.amazonaws.com"
    )
    AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "max-age=86400"}
    AWS_IS_GZIPPED = True
    AWS_ENABLED = True
    AWS_S3_SECURE_URLS = True
    DEFAULT_S3_PATH = "media"
    MEDIA_ROOT = f"/{DEFAULT_S3_PATH}/"
    MEDIA_URL = f"//{S3_DOMAIN}/{DEFAULT_S3_PATH}/"
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3.S3Storage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
else:
    AWS_ENABLED = False
    MEDIA_ROOT = os.environ.get("MEDIA_ROOT", str(BASE_DIR / "media"))
    MEDIA_URL = os.environ.get("MEDIA_URL", "/media/")


# EMAIL_BACKEND and the SES region are configured in the base settings. Merely
# selecting production mode must never force live email delivery.

_session_cookie_domain = os.environ.get("SESSION_COOKIE_DOMAIN", "").strip()
if _session_cookie_domain:
    SESSION_COOKIE_DOMAIN = _session_cookie_domain

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True


SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        send_default_pii=_env_bool("SENTRY_SEND_DEFAULT_PII", default=False),
    )

RAVEN_CONFIG = {"dsn": SENTRY_DSN} if SENTRY_DSN else {}
