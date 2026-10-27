from django.apps import AppConfig


class PluginsConfig(AppConfig):
    name = "apps.plugins"
    label = "plugins"
    verbose_name = "Plugins"

    def ready(self):
        from .loader import load_all

        load_all()
