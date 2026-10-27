import json

from django import template
from django.utils.html import format_html

register = template.Library()

STATUS_COLORS = {
    "new": "primary", "updated": "info", "duplicate": "secondary", "filtered_out": "light",
    "extraction_failed": "danger", "needs_attention": "warning", "unrouted": "dark",
    "healthy": "success", "degraded": "warning", "silent": "secondary", "failing": "danger",
    "unknown": "light", "disabled": "light", "running": "info", "success": "success", "partial": "warning",
    "failed": "danger", "skipped": "secondary",
}


@register.filter
def status_badge(value, label=None):
    color = STATUS_COLORS.get(value, "secondary")
    text_class = " text-dark" if color in {"light", "warning", "info"} else ""
    return format_html('<span class="badge text-bg-{}{}">{}</span>', color, text_class, label or value)


@register.filter
def pretty_json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


@register.filter
def short_value(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= 120 else text[:117] + "…"


RESULT_KEYS = ["new", "updated", "duplicate", "unchanged", "filtered_out", "needs_attention", "extraction_failed",
               "skipped_old", "disallowed"]


@register.filter
def result_counts(stats):
    """Headline counts of a run's stats dict (avoids the template '.items' key clash)."""
    return [(key, stats[key]) for key in RESULT_KEYS if (stats or {}).get(key)]


@register.filter
def get_item(mapping, key):
    return (mapping or {}).get(key)


@register.simple_tag
def model_label(obj):
    return obj._meta.label_lower
