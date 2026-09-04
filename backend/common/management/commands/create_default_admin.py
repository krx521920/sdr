import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()

_TRUE_VALUES = {"1", "true", "yes", "on"}
_PRODUCTION_ENV_TYPES = {"prod", "production"}
_KNOWN_DEFAULT_PASSWORDS = {"admin", "admin123", "change-me", "password"}


class Command(BaseCommand):
    help = "Create a default superuser if none exists (for Docker bootstrap)"

    def handle(self, *args, **options):
        env_type = os.environ.get("ENV_TYPE", "dev").strip().lower()
        is_production = env_type in _PRODUCTION_ENV_TYPES
        create_setting = os.environ.get("CREATE_DEFAULT_ADMIN")
        should_create = bool(
            create_setting is not None
            and create_setting.strip().lower() in _TRUE_VALUES
        )

        if not should_create:
            self.stdout.write(
                self.style.WARNING(
                    "Default admin bootstrap is disabled. Use createsuperuser, or set "
                    "CREATE_DEFAULT_ADMIN=true with a strong ADMIN_PASSWORD."
                )
            )
            return

        if User.objects.filter(is_superuser=True).exists():
            self.stdout.write(self.style.SUCCESS("Superuser already exists — skipping."))
            return

        email = os.environ.get("ADMIN_EMAIL", "admin@localhost")
        password = os.environ.get("ADMIN_PASSWORD", "")

        if not password:
            if is_production:
                raise CommandError(
                    "ADMIN_PASSWORD is required when CREATE_DEFAULT_ADMIN=true in production."
                )
            self.stdout.write(
                self.style.WARNING(
                    "WARNING: ADMIN_PASSWORD not set — using default 'admin'. "
                    "Change it immediately in production!"
                )
            )
            password = "admin"

        if is_production:
            if (
                len(password.strip()) < 12
                or password.strip().lower() in _KNOWN_DEFAULT_PASSWORDS
            ):
                raise CommandError(
                    "ADMIN_PASSWORD must contain at least 12 characters after trimming "
                    "outer whitespace and must not be a known default when "
                    "CREATE_DEFAULT_ADMIN=true in production."
                )
            try:
                validate_password(password, user=User(email=email))
            except ValidationError as exc:
                raise CommandError(
                    "ADMIN_PASSWORD does not satisfy the configured Django password "
                    "validators."
                ) from exc

        User.objects.create_superuser(
            email=email,
            password=password,
        )
        self.stdout.write(self.style.SUCCESS(f"Created default superuser: {email}"))
