from dataclasses import dataclass, field

from apps.plugins.registry import Plugin


@dataclass
class Acquired:
    """One document handed from an acquisition method to the pipeline."""

    data: bytes
    url: str = ""
    filename: str = ""
    content_type: str = ""
    headers: dict = field(default_factory=dict)
    origin: str = "http"  # RawArtifact.ORIGINS
    link_text: str = ""
    parent: object = None  # RawArtifact this came out of (zip / email)
    hints: dict = field(default_factory=dict)  # defaults applied before rules (title, published_at...)
    overrides: dict = field(default_factory=dict)  # forced after rules (operator-provided values)
    manual: bool = False
    flags: list = field(default_factory=list)  # quality flags from acquisition (e.g. js_not_rendered)
    attention: list = field(default_factory=list)  # reasons a human should look at the resulting messages
    append_texts: list = field(default_factory=list)  # [(label, text)] appended to the body (email + attachments)
    process_children: bool = True  # process zip members / email attachments as their own documents
    force_status: str = ""  # e.g. "unrouted" for emails no mailbox rule matched
    bypass_type_check: bool = False  # the file was already chosen explicitly (mailbox rule)


class FetcherPlugin(Plugin):
    """An acquisition method. ``collect(ctx)`` yields Acquired documents."""

    kind = "fetcher"
    requires_entry_urls = True
    schedulable = True

    def collect(self, ctx):
        raise NotImplementedError
