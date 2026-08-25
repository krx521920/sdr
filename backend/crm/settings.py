import os
from datetime import timedelta

from corsheaders.defaults import default_headers
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    "SECRET_KEY", "django-insecure-dev-key-please-change-in-production"
)

if not SECRET_KEY or SECRET_KEY.startswith("django-insecure"):
    if os.environ.get("ENV_TYPE", "dev") != "dev":
        raise ValueError(
            "SECRET_KEY must be set to a secure value in non-dev environments"
        )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DEBUG", "False").lower() == "true"

# Security: Restrict allowed hosts - set ALLOWED_HOSTS env var in production
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    # Required for refresh-token rotation: simplejwt only defines
    # RefreshToken.blacklist()/check_blacklist() when this app is installed, so
    # without it BLACKLIST_AFTER_ROTATION below is silently a no-op.
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_ses",
    "drf_spectacular",
    "common",
    # New bounded contexts. Existing CRM tables stay in their original apps
    # while provider-owned configuration lives with its integration module.
    "sdr.apps.SDRConfig",
    "integrations.apps.IntegrationsConfig",
    "automation.apps.AutomationConfig",
    "matching.apps.MatchingConfig",
    "accounts",
    "cases",
    "contacts",
    "leads",
    "opportunity",
    "tasks",
    "invoices",
    "orders",
    "business_hours",
    "macros",
    # "teams",  # Merged into common app
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",  # CSRF protection
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "crum.CurrentRequestUserMiddleware",
    "common.middleware.get_company.GetProfileAndOrg",
    "common.middleware.rls_context.RequireOrgContext",  # RLS: Enforce org context + set PostgreSQL session variable
]

ROOT_URLCONF = "crm.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            os.path.join(BASE_DIR, "templates"),
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "common.context_processors.common.app_name",
                # "django_settings_export.settings_export",
            ],
        },
    },
]

WSGI_APPLICATION = "crm.wsgi.application"

# Database
# https://docs.djangoproject.com/en/1.10/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DBNAME", "crm_db"),
        "USER": os.environ.get("DBUSER", "postgres"),
        "PASSWORD": os.environ.get("DBPASSWORD", "postgres"),
        "HOST": os.environ.get("DBHOST", "localhost"),
        "PORT": os.environ.get("DBPORT", "5432"),
    }
}


# Password validation
# https://docs.djangoproject.com/en/1.10/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/1.10/topics/i18n/


TIME_ZONE = "Asia/Kolkata"

USE_I18N = True

USE_TZ = True

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)

AUTH_USER_MODEL = "common.User"

STATIC_ROOT = os.environ.get("STATIC_ROOT", os.path.join(BASE_DIR, "staticfiles"))
STATIC_URL = "/static/"
STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]

ENV_TYPE = os.environ.get("ENV_TYPE", "dev").strip().lower()
IS_PRODUCTION = ENV_TYPE in {"prod", "production"}
if ENV_TYPE == "dev":
    MEDIA_ROOT = os.path.join(BASE_DIR, "media")
    MEDIA_URL = "/media/"
elif IS_PRODUCTION:
    from .server_settings import *  # noqa: F401,F403

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@localhost")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@localhost")

# AWS SES settings (loaded when EMAIL_BACKEND is django_ses.SESBackend)
if "django_ses" in EMAIL_BACKEND:
    AWS_SES_REGION_NAME = os.environ.get("AWS_SES_REGION_NAME", "ap-south-1")
    AWS_SES_REGION_ENDPOINT = os.environ.get(
        "AWS_SES_REGION_ENDPOINT", f"email.{AWS_SES_REGION_NAME}.amazonaws.com"
    )
    AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET")
    # Uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from env if set;
    # otherwise falls back to IAM role credentials.


# celery Tasks
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND", "redis://localhost:6379/0"
)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
        "require_debug_true": {
            "()": "django.utils.log.RequireDebugTrue",
        },
    },
    "formatters": {
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[%(server_time)s] %(message)s",
        },
        "security": {
            "format": "%(asctime)s | %(levelname)s | %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "filters": ["require_debug_true"],
            "class": "logging.StreamHandler",
        },
        "console_debug_false": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "logging.StreamHandler",
        },
        "django.server": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "django.server",
        },
        "mail_admins": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "django.utils.log.AdminEmailHandler",
        },
        "logfile": {
            "class": "logging.FileHandler",
            "filename": os.environ.get("SERVER_LOG_PATH", "server.log"),
        },
        "security_audit": {
            "class": "logging.FileHandler",
            "filename": os.environ.get(
                "SECURITY_AUDIT_LOG_PATH", "security_audit.log"
            ),
            "formatter": "security",
        },
    },
    "loggers": {
        "django": {
            "handlers": [
                "console",
                "console_debug_false",
                "logfile",
            ],
            "level": "INFO",
        },
        "django.server": {
            "handlers": ["django.server"],
            "level": "INFO",
            "propagate": False,
        },
        "security.audit": {
            "handlers": ["security_audit", "console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

APPLICATION_NAME = "bottlecrm"

SETTINGS_EXPORT = ["APPLICATION_NAME"]

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "common.pat_auth.PATAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "common.external_auth.APIKeyAuthentication",
        # "rest_framework.authentication.SessionAuthentication",
        # "rest_framework.authentication.BasicAuthentication",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 10,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


SPECTACULAR_SETTINGS = {
    "TITLE": "BottleCRM API",
    "DESCRIPTION": "Open source CRM application",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "PREPROCESSING_HOOKS": ["common.custom_openapi.preprocessing_filter_spec"],
    "ENUM_NAME_OVERRIDES": {
        # Role enums
        "ProfileRoleEnum": "common.utils.ROLES",
        "BoardMemberRoleEnum": "tasks.models.BoardMember.ROLE_CHOICES",
        # Priority enums
        "TaskPriorityEnum": "tasks.models.Task.PRIORITY_CHOICES",
        "CasePriorityEnum": "common.utils.PRIORITY_CHOICE",
        "BoardTaskPriorityEnum": "tasks.models.BoardTask.PRIORITY_CHOICES",
        # Status enums
        "TaskStatusEnum": "tasks.models.Task.STATUS_CHOICES",
        "CaseStatusEnum": "common.utils.STATUS_CHOICE",
        "SolutionStatusEnum": "cases.models.Solution.STATUS_CHOICES",
        "DocumentStatusEnum": "common.models.Document.DOCUMENT_STATUS_CHOICE",
        "InvoiceStatusEnum": "invoices.models.Invoice.INVOICE_STATUS",
        "ContactFormStatusEnum": "common.models.ContactFormSubmission.STATUS_CHOICES",
        "LeadStatusEnum": "common.utils.LEAD_STATUS",
    },
}

# JWT_SETTINGS = {
#     'bearerFormat': ('Bearer', 'jwt', 'Jwt')
# }

SWAGGER_SETTINGS = {
    "DEFAULT_INFO": "crm.urls.info",
    "SECURITY_DEFINITIONS": {
        "Bearer": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "description": "Enter 'Bearer <token>'",
        },
    },
}

CORS_ALLOW_HEADERS = default_headers + ("org",)
# Security: CORS configuration via environment variables
CORS_ORIGIN_ALLOW_ALL = os.environ.get("CORS_ALLOW_ALL", "False").lower() == "true"
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
# Security: CSRF trusted origins via environment variable
_csrf_origins = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]

# TLS is normally terminated by the deployment's reverse proxy. Keep these
# environment-controlled so local HTTP remains usable while public deployments
# can enforce secure redirects and cookies.
_secure_default = "True" if IS_PRODUCTION else "False"
SECURE_SSL_REDIRECT = (
    os.environ.get("SECURE_SSL_REDIRECT", _secure_default).lower() == "true"
)
SESSION_COOKIE_SECURE = (
    os.environ.get("SESSION_COOKIE_SECURE", _secure_default).lower() == "true"
)
CSRF_COOKIE_SECURE = (
    os.environ.get("CSRF_COOKIE_SECURE", _secure_default).lower() == "true"
)
if os.environ.get("TRUST_X_FORWARDED_PROTO", _secure_default).lower() == "true":
    # The deployment proxy must overwrite (not append to) this header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Security: HSTS with 1 year duration (recommended minimum)
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
# STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'

SIMPLE_JWT = {
    # Security: Reduced token lifetimes
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    # Security: Enable token rotation to invalidate old refresh tokens
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "VERIFYING_KEY": None,
    "AUDIENCE": None,
    "ISSUER": None,
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}
# it is needed in custome middlewere to get the user from the token
JWT_ALGO = "HS256"


DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "http://localhost:8000")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
SDR_NURTURE_TRACKING_BASE_URL = os.environ.get(
    "SDR_NURTURE_TRACKING_BASE_URL",
    FRONTEND_URL,
).rstrip("/")
SDR_NURTURE_TRACKING_MAX_AGE_SECONDS = int(
    os.environ.get("SDR_NURTURE_TRACKING_MAX_AGE_SECONDS", str(366 * 24 * 60 * 60))
)
SWAGGER_ROOT_URL = os.environ.get("SWAGGER_ROOT_URL", "http://localhost:8000")

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

# Meta Lead Ads integration. Pin the Graph version so provider changes are
# deployed intentionally rather than arriving implicitly.
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
META_WEBHOOK_VERIFY_TOKEN = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.environ.get(
    "WHATSAPP_WEBHOOK_VERIFY_TOKEN",
    META_WEBHOOK_VERIFY_TOKEN,
)
META_GRAPH_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", "v25.0")
META_GRAPH_API_BASE_URL = os.environ.get(
    "META_GRAPH_API_BASE_URL", "https://graph.facebook.com"
)
META_GRAPH_API_TIMEOUT = float(os.environ.get("META_GRAPH_API_TIMEOUT", "10"))
META_OAUTH_DIALOG_URL = os.environ.get(
    "META_OAUTH_DIALOG_URL",
    f"https://www.facebook.com/{META_GRAPH_API_VERSION}/dialog/oauth",
)
META_OAUTH_REDIRECT_URI = os.environ.get(
    "META_OAUTH_REDIRECT_URI",
    f"{DOMAIN_NAME.rstrip('/')}/api/integrations/facebook/oauth/callback/",
)
META_OAUTH_FRONTEND_REDIRECT_URL = os.environ.get(
    "META_OAUTH_FRONTEND_REDIRECT_URL",
    f"{FRONTEND_URL.rstrip('/')}/settings/facebook",
)
META_OAUTH_STATE_TTL = int(os.environ.get("META_OAUTH_STATE_TTL", "900"))
META_OAUTH_SCOPES = tuple(
    scope.strip()
    for scope in os.environ.get(
        "META_OAUTH_SCOPES",
        "pages_show_list,pages_manage_metadata,leads_retrieval",
    ).split(",")
    if scope.strip()
)

# OpenAI defaults for the SDR model gateway. Provider URLs and allow-lists are
# deployment-owned; optional tenant keys are handled separately and encrypted.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_BASE_URL = os.environ.get(
    "OPENAI_API_BASE_URL", "https://api.openai.com/v1"
).rstrip("/")
OPENAI_API_TIMEOUT_SECONDS = max(
    5, int(os.environ.get("OPENAI_API_TIMEOUT_SECONDS", "30"))
)
OPENAI_ALLOWED_MODELS = tuple(
    value.strip()
    for value in os.environ.get(
        "OPENAI_ALLOWED_MODELS",
        "gpt-5.6-luna,gpt-5.6-terra,gpt-5.6-sol",
    ).split(",")
    if value.strip()
) or ("gpt-5.6-luna",)
OPENAI_ALLOWED_REASONING_EFFORTS = tuple(
    value.strip()
    for value in os.environ.get(
        "OPENAI_ALLOWED_REASONING_EFFORTS", "none,low,medium"
    ).split(",")
    if value.strip() in {"none", "low", "medium", "high", "xhigh", "max"}
) or ("low",)

# The gateway owns provider URLs and model allow-lists. Tenant administrators
# can select only from these values and can never redirect credentials to an
# arbitrary host.
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", os.environ.get("ARK_API_KEY", ""))
DOUBAO_API_BASE_URL = os.environ.get(
    "DOUBAO_API_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
).rstrip("/")
DOUBAO_API_TIMEOUT_SECONDS = max(
    5, int(os.environ.get("DOUBAO_API_TIMEOUT_SECONDS", "30"))
)
DOUBAO_ALLOWED_MODELS = tuple(
    value.strip()
    for value in os.environ.get(
        "DOUBAO_ALLOWED_MODELS", "doubao-seed-2-0-lite-260215"
    ).split(",")
    if value.strip()
) or ("doubao-seed-2-0-lite-260215",)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE_URL = os.environ.get(
    "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com"
).rstrip("/")
DEEPSEEK_API_TIMEOUT_SECONDS = max(
    5, int(os.environ.get("DEEPSEEK_API_TIMEOUT_SECONDS", "30"))
)
DEEPSEEK_ALLOWED_MODELS = tuple(
    value.strip()
    for value in os.environ.get(
        "DEEPSEEK_ALLOWED_MODELS", "deepseek-v4-flash,deepseek-v4-pro"
    ).split(",")
    if value.strip()
) or ("deepseek-v4-flash",)

AI_GATEWAY_ALLOW_TENANT_KEYS = (
    os.environ.get("AI_GATEWAY_ALLOW_TENANT_KEYS", "True").lower() == "true"
)
AI_GATEWAY_ALLOWED_REASONING_EFFORTS = tuple(
    value.strip()
    for value in os.environ.get(
        "AI_GATEWAY_ALLOWED_REASONING_EFFORTS",
        ",".join(OPENAI_ALLOWED_REASONING_EFFORTS),
    ).split(",")
    if value.strip() in {"none", "low", "medium", "high", "xhigh", "max"}
) or ("low",)

# Apollo People Search is tenant-authenticated. The provider URL remains a
# deployment-owned setting so tenant credentials cannot redirect requests.
APOLLO_API_BASE_URL = os.environ.get(
    "APOLLO_API_BASE_URL", "https://api.apollo.io/api/v1"
).rstrip("/")
APOLLO_API_TIMEOUT = max(
    1.0,
    min(60.0, float(os.environ.get("APOLLO_API_TIMEOUT", "15"))),
)

# LinkedIn Invitations API is restricted to approved partners. The base URL is
# deployment-owned so tenant tokens cannot redirect outbound requests.
LINKEDIN_API_BASE_URL = os.environ.get(
    "LINKEDIN_API_BASE_URL", "https://api.linkedin.com"
).rstrip("/")
LINKEDIN_API_TIMEOUT = max(
    1.0,
    min(60.0, float(os.environ.get("LINKEDIN_API_TIMEOUT", "10"))),
)

# Feishu host ownership stays with deployment configuration. Tenant credentials
# may authenticate but cannot redirect server-side requests to another host.
FEISHU_OPEN_API_BASE_URL = os.environ.get(
    "FEISHU_OPEN_API_BASE_URL", "https://open.feishu.cn"
).rstrip("/")
FEISHU_OPEN_API_TIMEOUT = max(
    1.0,
    min(60.0, float(os.environ.get("FEISHU_OPEN_API_TIMEOUT", "15"))),
)

# Durable automation jobs are persisted before broker dispatch. Handlers are
# deployment-owned allow-list entries rather than arbitrary dotted paths from
# user input.
AUTOMATION_JOB_HANDLERS = {
    "facebook.process_lead": (
        "integrations.providers.facebook.jobs.process_facebook_lead_job"
    ),
    "facebook.send_conversion_event": (
        "integrations.providers.facebook.conversions.process_facebook_conversion_job"
    ),
    "facebook.process_messenger_message": (
        "integrations.providers.facebook.messenger.process_facebook_messenger_job"
    ),
    "facebook.send_messenger_reply": (
        "integrations.providers.facebook.messenger.process_facebook_messenger_reply_job"
    ),
    "whatsapp.send_campaign_message": (
        "integrations.providers.whatsapp.outbound.process_whatsapp_message_job"
    ),
    "linkedin.send_campaign_invitation": (
        "integrations.providers.linkedin.outbound.process_linkedin_invitation_job"
    ),
    "feishu_base.sync_research_result": (
        "integrations.providers.feishu_base.sync.process_feishu_base_sync_job"
    ),
    "sdr.process_intake": (
        "integrations.providers.website.jobs.process_website_intake_job"
    ),
    "sdr.send_acknowledgement": "sdr.response.process_acknowledgement_email_job",
    "sdr.notify_sales_in_app": "sdr.response.process_sales_in_app_job",
    "sdr.notify_sales_feishu": "sdr.response.process_sales_feishu_job",
    "sdr.send_nurture_email": "sdr.nurture.process_nurture_email_job",
    "sdr.process_inbound_email": "sdr.email.process_inbound_email_job",
    "sdr.process_outbound_prospect": "sdr.outbound.process_outbound_prospect_job",
    "sdr.sync_outbound_source": "sdr.sources.process_outbound_source_sync_job",
    "sdr.generate_outbound_copy": "sdr.outbound_copy.process_outbound_copy_job",
    "matching.recompute_opportunity": "matching.jobs.process_recompute_opportunity_job",
}
INBOUND_EMAIL_ROUTE_HANDLERS = {
    "sdr": "sdr.email.enqueue_inbound_email",
}
AUTOMATION_RETRY_BASE_SECONDS = max(
    1, int(os.environ.get("AUTOMATION_RETRY_BASE_SECONDS", "5"))
)
AUTOMATION_RETRY_MAX_SECONDS = max(
    AUTOMATION_RETRY_BASE_SECONDS,
    int(os.environ.get("AUTOMATION_RETRY_MAX_SECONDS", "900")),
)
AUTOMATION_JOB_LEASE_SECONDS = max(
    60, int(os.environ.get("AUTOMATION_JOB_LEASE_SECONDS", "600"))
)
AUTOMATION_MANUAL_RETRY_ATTEMPTS = max(
    1, int(os.environ.get("AUTOMATION_MANUAL_RETRY_ATTEMPTS", "3"))
)
SDR_FEISHU_TIMEOUT_SECONDS = max(
    1, min(15, int(os.environ.get("SDR_FEISHU_TIMEOUT_SECONDS", "5")))
)

# Generate once with a trusted Fernet key generator and store the result in the
# deployment secret manager. Local development may omit this and derive a key
# from SECRET_KEY; production must use an independent, stable key so Django key
# rotation cannot make stored integration credentials unreadable.
INTEGRATION_ENCRYPTION_KEY = os.environ.get("INTEGRATION_ENCRYPTION_KEY", "").strip()
if IS_PRODUCTION and not INTEGRATION_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        "INTEGRATION_ENCRYPTION_KEY is required in production"
    )
if INTEGRATION_ENCRYPTION_KEY:
    try:
        Fernet(INTEGRATION_ENCRYPTION_KEY.encode("ascii"))
    except (TypeError, UnicodeError, ValueError):
        raise ImproperlyConfigured(
            "INTEGRATION_ENCRYPTION_KEY must be a valid Fernet key"
        ) from None
