from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "Core"

    def ready(self):
        from django.conf import settings

        from . import versioning

        versioning.connect_signals()
        if settings.EWCE_USE_SYSTEM_CERTS:
            # Verify TLS against the Windows certificate store, which (unlike certifi) completes
            # chains for sites that omit their intermediate certificate (common on gov.bd).
            try:
                import truststore

                truststore.inject_into_ssl()
            except ImportError:
                pass
