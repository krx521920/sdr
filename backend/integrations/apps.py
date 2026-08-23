from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "integrations"
    verbose_name = "Integrations"

    def ready(self):
        from integrations import signals  # noqa: F401
        from integrations.providers.sdr_adapters import register_sdr_provider_adapters

        register_sdr_provider_adapters()
