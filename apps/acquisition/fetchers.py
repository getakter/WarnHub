"""Built-in acquisition methods."""
import datetime as dt
from calendar import timegm
from html import escape

from apps.plugins.registry import register

from .base import Acquired, FetcherPlugin
from .crawl import DOCUMENT_TYPES, discover_links, expand_entry_url, guess_type, in_scope, iter_pages
from .http import FetchError, PoliteClient

CALLABLE_PATTERN = r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$"


def _client(ctx):
    return PoliteClient(ctx.settings["fetch"], log=ctx.log)


def _entry_urls(ctx):
    tz = ctx.settings["content"].get("timezone", "Asia/Dhaka")
    urls = [expand_entry_url(u, tz) for u in (ctx.feed.entry_urls or [])]
    if not urls:
        ctx.log("error", "This feed has no entry URLs. Add one under Edit feed → Entry URLs.")
    return urls


def _acquired(result, **extra):
    return Acquired(data=result.content, url=result.url, content_type=result.content_type, headers=result.headers,
                    flags=list(result.flags), **extra)


@register
class WebPageFetcher(FetcherPlugin):
    name = "web_page"
    label = "Web page"
    description = "Fetch each entry URL (page or file) and run the extraction rules on it."
    config_schema = {
        "type": "object",
        "properties": {
            "follow_pagination": {"type": "boolean", "default": False, "title": "Follow pagination (see URL scope)"},
        },
    }

    def collect(self, ctx):
        client = _client(ctx)
        pagination = ctx.settings["scope"].get("pagination")
        for url in _entry_urls(ctx):
            for result in iter_pages(client, url, pagination, ctx, enabled=self.config.get("follow_pagination")):
                yield _acquired(result)


@register
class ListingToDocumentsFetcher(FetcherPlugin):
    name = "listing_to_documents"
    label = "Listing page → documents"
    description = "Read a listing page, discover document links (PDF/Word/pages) within the URL scope and collect each."
    config_schema = {
        "type": "object",
        "properties": {
            "item_selector": {"type": "string", "format": "css-selector", "title": "Restrict to items",
                              "description": "Optional: only look for links inside these elements (rows/cards)."},
            "item_date_selector": {"type": "string", "format": "css-selector", "title": "Date inside item",
                                   "description": "Optional: used with the lookback window to skip old items."},
            "link_text_as_title": {"type": "boolean", "default": True, "title": "Use link text as title"},
            "also_store_listing": {"type": "boolean", "default": False,
                                   "title": "Also save the listing page itself as a message"},
            "skip_known_urls": {"type": "boolean", "default": True,
                                "title": "Skip documents already collected from the same URL",
                                "description": "Turn off to detect changed files that keep the same URL."},
        },
    }

    def collect(self, ctx):
        from apps.collection.models import CollectedMessage, RawArtifact
        from apps.extraction.dates import parse_date

        cfg, scope, files = self.config, ctx.settings["scope"], ctx.settings["files"]
        tz = ctx.settings["content"].get("timezone", "Asia/Dhaka")
        client = _client(ctx)
        known = set()
        if cfg.get("skip_known_urls", True) and not ctx.dry_run:
            known = set(CollectedMessage.objects.filter(feed=ctx.feed).values_list("source_url", flat=True))
            known |= set(RawArtifact.objects.filter(first_feed=ctx.feed).values_list("source_url", flat=True))
        max_depth = int(scope.get("max_depth", 1))
        seen = set()
        for entry in _entry_urls(ctx):
            queue = [(page, 0) for page in iter_pages(client, entry, scope.get("pagination"), ctx)]
            while queue:
                page, depth = queue.pop(0)
                if cfg.get("also_store_listing") and depth == 0:
                    yield _acquired(page)
                html = page.content.decode("utf-8", errors="replace")
                links = discover_links(html, page.url, scope, item_selector=cfg.get("item_selector", "") if depth == 0 else "",
                                       item_date_selector=cfg.get("item_date_selector", ""))
                ctx.log("info", f"Found {len(links)} links on {page.url}")
                ctx.stats["links_found"] += len(links)
                for link in links:
                    url = link["url"]
                    if url in seen or url == page.url:
                        continue
                    seen.add(url)
                    ok, reason = in_scope(url, link["text"], scope, entry)
                    if not ok:
                        ctx.stats["out_of_scope"] += 1
                        continue
                    guessed = guess_type(url)
                    if guessed and guessed not in files.get("allowed_types", []) and files.get("disallowed_action") == "ignore":
                        ctx.stats["disallowed"] += 1
                        ctx.log("info", f"Skipped {url}: file type '{guessed}' not allowed for this feed")
                        continue
                    if url in known:
                        ctx.stats["skipped_known"] += 1
                        continue
                    published = parse_date(link["date_text"], tz=tz) if link["date_text"] else None
                    if published and ctx.cutoff and published < ctx.cutoff:
                        ctx.stats["skipped_old"] += 1
                        continue
                    if ctx.limit_reached:
                        ctx.log("info", "Max items per run reached; stopping link discovery.")
                        return
                    try:
                        result = client.get(url)
                    except FetchError as exc:
                        ctx.log("error", str(exc), url=url)
                        continue
                    hints = {"published_at": published, "published_at_text": link["date_text"]}
                    if cfg.get("link_text_as_title", True) and link["text"]:
                        hints["title"] = link["text"]
                    yield _acquired(result, link_text=link["text"], hints=hints)
                    is_page = guess_type(result.url) not in DOCUMENT_TYPES and "html" in (result.content_type or "html")
                    if is_page and depth + 1 < max_depth:
                        queue.append((result, depth + 1))


def _entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return dt.datetime.fromtimestamp(timegm(value), tz=dt.timezone.utc)
    return None


@register
class RssAtomFetcher(FetcherPlugin):
    name = "rss_atom"
    label = "RSS / Atom feed"
    description = "Read an RSS or Atom feed; each entry becomes a message."
    config_schema = {
        "type": "object",
        "properties": {
            "use_entry_content": {"type": "boolean", "default": True, "title": "Use entry content/summary as body"},
            "follow_entry_links": {"type": "boolean", "default": False,
                                   "title": "Fetch each entry's link and extract the page instead"},
        },
    }

    def collect(self, ctx):
        import feedparser

        client = _client(ctx)
        for url in _entry_urls(ctx):
            try:
                result = client.get(url)
            except FetchError as exc:
                ctx.log("error", str(exc), url=url)
                continue
            parsed = feedparser.parse(result.content)
            if parsed.bozo and not parsed.entries:
                ctx.log("error", f"Not a readable RSS/Atom feed: {parsed.get('bozo_exception')}", url=url)
                continue
            ctx.log("info", f"Feed has {len(parsed.entries)} entries", url=url)
            for entry in parsed.entries:
                if ctx.limit_reached:
                    return
                link = entry.get("link", "") or url
                published = _entry_datetime(entry)
                hints = {"title": entry.get("title", ""), "published_at": published,
                         "published_at_text": entry.get("published") or entry.get("updated") or "",
                         "source_item_key": entry.get("id") or link}
                if self.config.get("follow_entry_links") and entry.get("link"):
                    try:
                        page = client.get(entry["link"])
                    except FetchError as exc:
                        ctx.log("error", str(exc), url=entry["link"])
                        continue
                    yield _acquired(page, link_text=hints["title"], hints=hints)
                    continue
                content = ""
                if self.config.get("use_entry_content", True):
                    if entry.get("content"):
                        content = entry["content"][0].get("value", "")
                    content = content or entry.get("summary", "")
                html = (f"<html><head><title>{escape(hints['title'])}</title></head>"
                        f"<body><h1>{escape(hints['title'])}</h1>{content}</body></html>")
                yield Acquired(data=html.encode("utf-8"), url=link, content_type="text/html; charset=utf-8",
                               link_text=hints["title"], hints=hints, headers={"x-feed-url": url})


@register
class EmailFetcher(FetcherPlugin):
    name = "email"
    label = "Email (fed by mailbox rules)"
    description = "Receives emails routed by Mailbox rules. Configure the mailbox and its routing rules."
    requires_entry_urls = False
    schedulable = False


@register
class ManualUploadFetcher(FetcherPlugin):
    name = "manual_upload"
    label = "Manual upload / paste only"
    description = "Operators upload files or paste text into this feed."
    requires_entry_urls = False
    schedulable = False


@register
class CustomPluginFetcher(FetcherPlugin):
    name = "custom_plugin"
    label = "Custom Python fetcher"
    description = "Call a generator function from custom_plugins/ that yields documents."
    requires_entry_urls = False
    config_schema = {
        "type": "object",
        "required": ["callable"],
        "properties": {
            "callable": {"type": "string", "pattern": CALLABLE_PATTERN, "title": "Function",
                         "description": "module:function, called as function(ctx, options); yields Acquired or dicts"},
            "options": {"type": "object", "x-widget": "json", "default": {}},
        },
    }

    def collect(self, ctx):
        from apps.extraction.rules import resolve_callable

        function = resolve_callable(self.config["callable"])
        for doc in function(ctx, self.config.get("options") or {}):
            yield doc if isinstance(doc, Acquired) else Acquired(**doc)
