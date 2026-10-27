"""Crawl helpers: entry-URL placeholders, URL scope rules, link discovery and pagination."""
from urllib.parse import urldefrag, urljoin, urlparse
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.core.bangla import clean_text, nfc
from apps.extraction import select as sel
from apps.extraction.filetypes import extension_type, filename_from_url
from apps.extraction.patterns import matches_any

DOCUMENT_TYPES = {"pdf", "docx", "doc", "xlsx", "xls", "csv", "txt", "rtf", "image", "eml", "zip"}


def expand_entry_url(url, tz="Asia/Dhaka", now=None):
    now = (now or timezone.now()).astimezone(ZoneInfo(tz or "Asia/Dhaka"))
    return (url.replace("{YYYY}", f"{now:%Y}").replace("{YY}", f"{now:%y}").replace("{MM}", f"{now:%m}")
            .replace("{M}", str(now.month)).replace("{DD}", f"{now:%d}").replace("{D}", str(now.day)))


def _base_host(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url, base_url):
    a, b = _base_host(urlparse(url).hostname), _base_host(urlparse(base_url).hostname)
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def guess_type(url):
    """File type guessed from the URL alone (None when it looks like an ordinary page)."""
    return extension_type(filename_from_url(url))


def in_scope(url, link_text, scope, base_url):
    """Return (ok, reason)."""
    syntax = scope.get("pattern_syntax", "wildcard")
    path_only = urldefrag(url)[0].split("?", 1)[0]
    if scope.get("same_domain_only", True) and not same_site(url, base_url):
        return False, "other domain"
    include = scope.get("include_patterns") or []
    if include and not (matches_any(include, url, syntax) or matches_any(include, path_only, syntax)):
        return False, "not matching include patterns"
    exclude = scope.get("exclude_patterns") or []
    if exclude and (matches_any(exclude, url, syntax) or matches_any(exclude, path_only, syntax)):
        return False, "matches an exclude pattern"
    text = nfc(link_text or "").casefold()
    wanted = [nfc(t).casefold() for t in scope.get("link_text_include") or []]
    if wanted and not any(w in text for w in wanted):
        return False, "link text filter"
    if any(nfc(t).casefold() in text for t in scope.get("link_text_exclude") or []):
        return False, "link text excluded"
    return True, ""


def discover_links(html, base_url, scope, *, item_selector="", item_date_selector=""):
    """Links on a page as [{"url", "text", "date_text"}], de-duplicated, in page order (before scope)."""
    root = sel.parse(html)
    if root is None:
        return []
    containers = root.cssselect(item_selector) if item_selector else [root]
    link_selector = scope.get("link_selector") or "a[href]"
    links, seen = [], set()
    for container in containers:
        date_text = ""
        if item_selector and item_date_selector:
            nodes = container.cssselect(item_date_selector)
            date_text = sel.node_text(nodes[0]) if nodes else ""
        for anchor in container.cssselect(link_selector):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            url = urldefrag(urljoin(base_url, href))[0]
            if url in seen:
                continue
            seen.add(url)
            text = clean_text(anchor.text_content() or anchor.get("title") or "")
            if item_selector and not text:
                text = clean_text(sel.node_text(container))[:300]
            links.append({"url": url, "text": " ".join(text.split()), "date_text": date_text})
    return links


def next_page_url(html, current_url, next_selector):
    root = sel.parse(html)
    if root is None or not next_selector:
        return None
    for node in root.cssselect(next_selector):
        href = node.get("href") if node.tag == "a" else next((a.get("href") for a in node.iter("a") if a.get("href")), None)
        if href:
            return urldefrag(urljoin(current_url, href.strip()))[0]
    return None


def iter_pages(client, entry_url, pagination, ctx, *, enabled=True):
    """Yield FetchResult for the entry page and following pages (next-link selector or URL pattern)."""
    pagination = pagination or {}
    max_pages = int(pagination.get("max_pages", 1)) if enabled else 1
    pattern = pagination.get("url_pattern", "")
    start = int(pagination.get("start_page", 1))
    url, visited = entry_url, set()
    for index in range(max_pages):
        if not url or url in visited:
            return
        visited.add(url)
        try:
            result = client.get(url)
        except Exception as exc:  # noqa: BLE001 - reported on the run
            ctx.log("error", f"Listing page failed: {exc}", url=url)
            return
        yield result
        if index + 1 >= max_pages:
            return
        if pattern:
            url = pattern.replace("{page}", str(start + index + 1))
        else:
            html = result.content.decode("utf-8", errors="replace")
            url = next_page_url(html, result.url, pagination.get("next_selector", ""))
