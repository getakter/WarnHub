"""Example custom plugin. Every *.py file in custom_plugins/ is loaded at startup.

Copy this file, rename the classes and `name`s, restart `runserver` and `run_worker`, and the
new rule type / field extractor appears in the UI with a form generated from `config_schema`.
Files starting with "_" are ignored. A broken plugin is reported on the dashboard instead of
stopping the application.
"""
import re

from apps.extraction.base import ExtractorPlugin, FieldExtractorPlugin
from apps.plugins.registry import register


@register
class StripPhrasesRule(ExtractorPlugin):
    """Example rule type: removes admin-listed boilerplate phrases from every item's body."""

    name = "example_strip_phrases"
    label = "Example: strip boilerplate phrases"
    description = "Removes listed phrases (e.g. 'Visitor counter', 'Page last updated') from the body text."
    builtin = False
    config_schema = {
        "type": "object",
        "properties": {
            "phrases": {"type": "array", "items": {"type": "string"}, "title": "Phrases to remove",
                        "default": ["Visitor counter"]},
        },
    }

    def apply(self, items, ctx):
        for item in items:
            for phrase in self.config.get("phrases", []):
                item.body_text = item.body_text.replace(phrase, "")
        return items


@register
class FirstNumberField(FieldExtractorPlugin):
    """Example field extractor: the first number appearing after a label."""

    name = "example_first_number_after"
    label = "Example: first number after a label"
    builtin = False
    config_schema = {
        "type": "object",
        "required": ["label"],
        "properties": {"label": {"type": "string", "title": "Label", "examples": ["Signal No"]}},
    }

    def extract(self, item, ctx):
        from apps.core.bangla import to_ascii_digits

        text = to_ascii_digits(item.body_text)
        index = text.find(self.config["label"])
        if index < 0:
            return None
        match = re.search(r"\d+", text[index:])
        return match.group(0) if match else None
