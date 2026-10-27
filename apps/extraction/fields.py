"""Custom-field extractors (values stored in CollectedMessage.extra_fields)."""
import re

from apps.core.bangla import clean_text, nfc
from apps.plugins.registry import register

from . import rule_schemas as S
from . import select as sel
from .base import FieldExtractorPlugin


def _post_regex(value, pattern):
    if not pattern or not value:
        return value
    match = re.search(pattern, value, re.IGNORECASE)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


def _item_root(item, ctx):
    return sel.parse(item.body_html or (ctx.document.html if ctx.document is not None else ""))


@register
class CssField(FieldExtractorPlugin):
    name = "css"
    label = "CSS selector"
    config_schema = S.FIELD_CSS

    def extract(self, item, ctx):
        root = _item_root(item, ctx)
        if root is None:
            return None
        cfg = self.config
        nodes = root.cssselect(cfg["selector"])
        values = [sel.node_value(n, cfg["take"], cfg.get("attribute", ""), ctx.url) for n in nodes]
        value = cfg.get("join_with", " ").join(v for v in values if v)
        return _post_regex(clean_text(value) if cfg["take"] == "text" else value, cfg.get("post_regex"))


@register
class XPathField(FieldExtractorPlugin):
    name = "xpath"
    label = "XPath"
    config_schema = S.FIELD_XPATH

    def extract(self, item, ctx):
        root = _item_root(item, ctx)
        if root is None:
            return None
        values = [sel.node_text(n) for n in sel.select(root, xpath=self.config["xpath"])]
        return _post_regex(" ".join(v for v in values if v), self.config.get("post_regex"))


@register
class RegexField(FieldExtractorPlugin):
    name = "regex"
    label = "Regex"
    config_schema = S.FIELD_REGEX

    def extract(self, item, ctx):
        cfg = self.config
        text = nfc(item.get(cfg.get("source_field", "body_text")) or "")
        match = re.search(cfg["pattern"], text, (re.IGNORECASE if cfg.get("ignore_case", True) else 0) | re.MULTILINE)
        if not match:
            return None
        group = cfg.get("group", "1")
        try:
            value = match.group(int(group)) if str(group).isdigit() else match.group(group)
        except IndexError:  # no such group number / name
            value = match.group(0)
        return clean_text(value) if value else None


@register
class LabelAfterField(FieldExtractorPlugin):
    name = "label_after"
    label = "Text after a label"
    description = "Value that follows a label such as 'Issue time:' or 'জারির সময়:'."
    config_schema = S.FIELD_LABEL_AFTER

    def extract(self, item, ctx):
        cfg = self.config
        text = nfc(item.get(cfg.get("source_field", "body_text")) or "")
        lowered = text.casefold()
        for label in cfg["labels"]:
            index = lowered.find(nfc(label).casefold())
            if index < 0:
                continue
            rest = text[index + len(nfc(label)):]
            stop = re.search(cfg.get("stop_at") or r"\n", rest)
            value = rest[: stop.start()] if stop else rest
            value = value[: cfg.get("max_chars", 200)].strip(cfg.get("strip_chars", ":：-–— \t"))
            if value.strip():
                return clean_text(value)
        return None


@register
class MetadataField(FieldExtractorPlugin):
    name = "metadata"
    label = "Document / email metadata"
    config_schema = S.FIELD_METADATA

    def extract(self, item, ctx):
        key = self.config["key"]
        if key == "url":
            return item.source_url or item.meta.get("url")
        value = item.meta.get(key)
        if value is None and key.startswith("http."):
            value = item.meta.get(key.lower())
        return str(value) if value not in (None, "") else None


@register
class ConstantField(FieldExtractorPlugin):
    name = "constant"
    label = "Constant value"
    config_schema = S.FIELD_CONSTANT

    def extract(self, item, ctx):
        return self.config["value"]
