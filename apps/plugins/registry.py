"""Plugin registry.

A plugin is a class with a ``kind``, a unique ``name`` and a JSON Schema ``config_schema``.
The admin UI auto-generates a validated form from the schema, so adding a plugin class
(built-in or dropped into ``custom_plugins/``) is all that is needed to make it configurable.
"""
import copy
import logging

logger = logging.getLogger("ewce.plugins")

KINDS = {
    "fetcher": "Acquisition method",
    "parser": "File parser",
    "extractor": "Extraction rule type",
    "field_extractor": "Custom field extractor",
    "mailbox_protocol": "Mailbox protocol",
}


class PluginNotFound(KeyError):
    def __str__(self):  # KeyError would wrap the message in quotes
        return str(self.args[0]) if self.args else ""


class Plugin:
    kind = None
    name = None
    label = None
    description = ""
    help_url = ""
    builtin = True
    config_schema = {"type": "object", "properties": {}}
    example_config = None

    def __init__(self, config=None):
        self.config = self.with_defaults(config or {})

    @classmethod
    def schema(cls):
        schema = copy.deepcopy(cls.config_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    @classmethod
    def default_config(cls):
        return schema_defaults(cls.schema())

    @classmethod
    def with_defaults(cls, config):
        from apps.core.merge import deep_merge

        return deep_merge(cls.default_config(), config)

    @classmethod
    def validate_config(cls, config):
        from .validation import validate

        return validate(cls.schema(), config)

    @classmethod
    def info(cls):
        return {
            "kind": cls.kind,
            "name": cls.name,
            "label": cls.label or cls.name,
            "description": cls.description,
            "builtin": cls.builtin,
            "module": cls.__module__,
        }


def schema_defaults(schema):
    if schema.get("type") == "object" and "properties" in schema:
        out = {}
        for key, sub in schema["properties"].items():
            if "default" in sub:
                out[key] = copy.deepcopy(sub["default"])
            elif sub.get("type") == "object" and "properties" in sub:
                nested = schema_defaults(sub)
                if nested:
                    out[key] = nested
        return out
    return copy.deepcopy(schema.get("default"))


class Registry:
    def __init__(self):
        self._plugins = {kind: {} for kind in KINDS}
        self.load_errors = []

    def register(self, cls):
        if cls.kind not in self._plugins:
            raise ValueError(f"Unknown plugin kind {cls.kind!r} on {cls.__name__}")
        if not cls.name:
            raise ValueError(f"Plugin {cls.__name__} has no name")
        existing = self._plugins[cls.kind].get(cls.name)
        if existing is not None and existing is not cls:
            logger.warning("plugin %s/%s overridden by %s", cls.kind, cls.name, cls.__module__)
        self._plugins[cls.kind][cls.name] = cls
        return cls

    def get(self, kind, name):
        try:
            return self._plugins[kind][name]
        except KeyError:
            raise PluginNotFound(f"No {KINDS.get(kind, kind)} plugin named {name!r}") from None

    def has(self, kind, name):
        return name in self._plugins.get(kind, {})

    def all(self, kind):
        return dict(sorted(self._plugins[kind].items()))

    def choices(self, kind):
        return [(name, cls.label or name) for name, cls in self.all(kind).items()]


registry = Registry()


def register(cls):
    """Class decorator: ``@register`` on a Plugin subclass."""
    return registry.register(cls)
