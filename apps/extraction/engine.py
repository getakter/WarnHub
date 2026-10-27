"""Rule engine: document -> items, then each enabled rule in order (items -> items).

Contract for rule plugins (``ExtractorPlugin.apply(items, ctx)``):
  * return the new list of items (may be the same list, modified);
  * call ``ctx.no_match("why")`` when the rule found nothing, so the rule's *on no match*
    setting decides what happens (continue / flag needs_attention / mark extraction_failed);
  * raise any exception for a real error; it becomes an ``extraction_failed`` message.
"""
import logging

from apps.core.merge import deep_merge
from apps.plugins.registry import PluginNotFound, registry

from .base import Item
from .patterns import matches

logger = logging.getLogger("ewce.extraction")

PARSE_RULE_TYPES = {"pdf": {"pdf"}, "office": {"docx", "xlsx", "xls", "csv"}, "email": {"eml"}}


class RuleError(Exception):
    def __init__(self, rule, message):
        self.rule = rule
        super().__init__(message)


class RuleContext:
    """What a rule plugin can see and do."""

    def __init__(self, *, document, rule, settings, url, file_type, log, feed=None):
        self.document = document
        self.rule = rule
        self.settings = settings
        self.url = url
        self.file_type = file_type
        self.feed = feed
        self._log = log
        self.matched = True
        self.no_match_reason = ""
        self._soup = None

    def no_match(self, reason):
        self.matched = False
        self.no_match_reason = reason

    def log(self, level, message, **context):
        self._log(level, f"[{self.rule.name if self.rule else 'field'}] {message}", **context)

    def soup(self, html=None):
        from bs4 import BeautifulSoup

        if html is not None:
            return BeautifulSoup(html, "lxml")
        if self._soup is None:
            self._soup = BeautifulSoup(self.document.html or "", "lxml")
        return self._soup


def rule_applies(rule, file_type, url):
    if rule.applies_to_types and file_type not in rule.applies_to_types:
        return False
    if rule.applies_to_url_pattern and not matches(rule.applies_to_url_pattern, url or ""):
        return False
    return True


def parse_options(rules, file_type, url):
    """Config of the first enabled parse-phase rule (PDF / Office / Email) that applies to this file."""
    for rule in rules:
        if not rule.enabled or rule.rule_type not in PARSE_RULE_TYPES:
            continue
        if file_type in PARSE_RULE_TYPES[rule.rule_type] and rule_applies(rule, file_type, url):
            return dict(rule.config or {}), rule
    return {}, None


def default_items(document, *, url="", link_text="", filename=""):
    meta = {"filename": filename, "url": url, "link_text": link_text, **document.metadata}
    if document.segments:
        return [
            Item(title=seg.get("title", ""), body_text=seg.get("text", ""), source_url=url, link_text=link_text,
                 fields=dict(seg.get("fields") or {}), meta=dict(meta), method="segment")
            for seg in document.segments
        ]
    return [Item(title=document.title, body_text=document.text, body_html=document.html, source_url=url,
                 link_text=link_text, meta=meta, method=f"parser:{document.file_type}")]


def apply_rules(document, items, rules, *, settings_for, url, file_type, log, feed=None):
    """Run item-phase rules in order. Raises RuleError when a rule fails or on_no_match='fail'."""
    for rule in rules:
        if not rule.enabled or rule.rule_type in PARSE_RULE_TYPES or not rule_applies(rule, file_type, url):
            continue
        try:
            plugin_cls = registry.get("extractor", rule.rule_type)
        except PluginNotFound as exc:
            raise RuleError(rule, str(exc)) from exc
        plugin = plugin_cls(rule.config or {})
        ctx = RuleContext(document=document, rule=rule, settings=settings_for(rule), url=url, file_type=file_type,
                          log=log, feed=feed)
        try:
            result = plugin.apply(items, ctx)
        except NotImplementedError:
            log("warning", f"Rule '{rule.name}' ({rule.rule_type}) is not implemented; skipped.")
            continue
        except Exception as exc:  # noqa: BLE001 - becomes extraction_failed with the reason
            raise RuleError(rule, f"Rule '{rule.name}' failed: {type(exc).__name__}: {exc}") from exc
        if not ctx.matched:
            reason = f"Rule '{rule.name}' found nothing: {ctx.no_match_reason}"
            if rule.on_no_match == "fail":
                raise RuleError(rule, reason)
            if rule.on_no_match == "flag":
                for item in items:
                    item.attention.append(reason)
                    item.flag("low_confidence")
            log("info", reason)
            continue
        items = list(result if result is not None else items)
        if getattr(plugin, "sets_extracting_rule", True):
            for item in items:
                item.rule_id = rule.pk
                item.method = rule.rule_type
    return items


def extract_custom_fields(items, fields, *, document, url, file_type, settings, log, feed=None):
    warned = set()
    for definition in fields:
        if not definition.enabled:
            continue
        if definition.applies_to_types and file_type not in definition.applies_to_types:
            continue
        try:
            plugin = registry.get("field_extractor", definition.extractor)(definition.config or {})
        except PluginNotFound as exc:
            log("error", f"Custom field '{definition.name}': {exc}")
            continue
        ctx = RuleContext(document=document, rule=None, settings=settings, url=url, file_type=file_type, log=log,
                          feed=feed)
        for item in items:
            try:
                value = plugin.extract(item, ctx)
            except NotImplementedError:
                if definition.extractor not in warned:
                    log("warning", f"Field extractor '{definition.extractor}' is not implemented; skipped.")
                    warned.add(definition.extractor)
                break
            except Exception as exc:  # noqa: BLE001 - a bad field must not lose the message
                log("warning", f"Custom field '{definition.name}' failed: {exc}")
                value = None
            if value not in (None, "", []):
                item.fields[definition.name] = value
            elif definition.required:
                item.flag("missing_required_field")
                item.attention.append(f"Required field '{definition.name}' not found")
    return items


def settings_resolver(base_settings):
    """settings_for(rule): feed effective settings + the rule's own override."""
    cache = {}

    def settings_for(rule):
        if not rule.settings_override:
            return base_settings
        if rule.pk not in cache:
            cache[rule.pk] = deep_merge(base_settings, rule.settings_override)
        return cache[rule.pk]

    return settings_for
