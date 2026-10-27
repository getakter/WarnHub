"""Shared types for parsers, extraction rules and custom-field extractors."""
from dataclasses import dataclass, field

from apps.plugins.registry import Plugin


@dataclass
class ParsedDocument:
    """What a parser makes of one artifact."""

    file_type: str
    text: str = ""
    html: str = ""
    title: str = ""
    metadata: dict = field(default_factory=dict)  # e.g. pdf metadata, email headers, meta tags
    font_names: list = field(default_factory=list)
    links: list = field(default_factory=list)  # [{"url", "text"}]
    attachments: list = field(default_factory=list)  # for eml / zip: [{"filename", "content_type", "data"}]
    segments: list = field(default_factory=list)  # pre-split parts, e.g. spreadsheet rows: [{"title", "text"}]
    quality_flags: list = field(default_factory=list)
    attention: list = field(default_factory=list)  # reasons a human should look at this document
    warnings: list = field(default_factory=list)
    pages: int = 0
    encoding: str = ""

    def flag(self, name):
        if name not in self.quality_flags:
            self.quality_flags.append(name)


@dataclass
class Item:
    """A candidate message flowing through the rule chain."""

    title: str = ""
    body_text: str = ""
    body_html: str = ""
    raw_text: str = ""  # untrusted text (e.g. legacy-font Bangla) kept out of body_text
    source_url: str = ""
    published_at_text: str = ""
    published_at: object = None
    source_item_key: str = ""
    link_text: str = ""
    fields: dict = field(default_factory=dict)  # custom / extra fields
    quality_flags: list = field(default_factory=list)
    attention: list = field(default_factory=list)
    rule_id: int | None = None
    method: str = ""
    confidence: float = 1.0
    meta: dict = field(default_factory=dict)

    CORE_FIELDS = ("title", "body_text", "body_html", "source_url", "published_at_text", "source_item_key", "link_text")

    def set(self, name, value):
        if name in self.CORE_FIELDS:
            setattr(self, name, value)
        else:
            self.fields[name] = value

    def get(self, name, default=""):
        if name in self.CORE_FIELDS:
            return getattr(self, name)
        return self.fields.get(name, default)

    def flag(self, name):
        if name not in self.quality_flags:
            self.quality_flags.append(name)


class ParserPlugin(Plugin):
    kind = "parser"
    file_types = ()
    file_type_hint = None  # set by the pipeline when one parser handles several types

    def parse(self, data, *, filename="", content_type="", url="", options=None, settings=None):
        raise NotImplementedError


class ExtractorPlugin(Plugin):
    """A rule type. ``phase='parse'`` rules only provide parser options (PDF, Office, email)."""

    kind = "extractor"
    phase = "items"
    produces_items = False

    def apply(self, items, ctx):
        """Transform a list of Items -> list of Items. ``ctx`` is a RuleContext."""
        raise NotImplementedError

    def parse_options(self):
        return self.config


class FieldExtractorPlugin(Plugin):
    kind = "field_extractor"

    def extract(self, item, ctx):
        raise NotImplementedError
