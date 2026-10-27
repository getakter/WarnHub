"""Resolve the effective (merged) configuration for a feed or a rule.

built-in defaults -> Source -> Format Profile -> Feed -> Rule (most specific wins)
"""
from apps.core.merge import merge_layers
from apps.plugins.registry import schema_defaults

from .settings_schema import SETTINGS_SCHEMA

LAYER_LABELS = {
    "default": "Built-in default",
    "source": "Source",
    "profile": "Format profile",
    "feed": "Feed",
    "rule": "Rule",
}


def builtin_defaults():
    return schema_defaults(SETTINGS_SCHEMA)


def feed_layers(feed, rule=None, *, upto=None):
    layers = [("default", builtin_defaults())]
    if feed.source_id:
        layers.append(("source", feed.source.settings_layer()))
    if feed.profile_id:
        layers.append(("profile", feed.profile.settings or {}))
    layers.append(("feed", feed.settings or {}))
    if rule is not None:
        layers.append(("rule", rule.settings_override or {}))
    if upto:
        names = [name for name, _ in layers]
        layers = layers[: names.index(upto)] if upto in names else layers
    return layers


def effective_settings(feed, rule=None, *, with_provenance=False, upto=None):
    merged, provenance = merge_layers(feed_layers(feed, rule, upto=upto))
    if with_provenance:
        return merged, provenance
    return merged


def provenance_rows(merged, provenance):
    """Flatten for display: [(path, value, layer_label)]."""
    rows = []

    def walk(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value:
                walk(value, path)
            else:
                layer = provenance.get(path, "default")
                rows.append((path, value, LAYER_LABELS.get(layer, layer), layer))

    walk(merged)
    return rows


def effective_method_config(feed):
    from apps.plugins.registry import registry

    plugin = registry.get("fetcher", feed.method)
    from apps.core.merge import deep_merge

    base = feed.profile.method_config if feed.profile_id and feed.profile.method == feed.method else {}
    return plugin.with_defaults(deep_merge(base or {}, feed.method_config or {}))
