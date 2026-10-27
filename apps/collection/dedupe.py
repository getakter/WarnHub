"""Item keys, content hashes and the duplicate / new-version decision.

same key + same hash            -> unchanged (sighting only: seen_count++, no new row)
same key + different hash       -> updated (new version superseding the old), or changed_ignored
new key  + hash seen in feed    -> duplicate (saved, linked with duplicate_of)
otherwise                       -> new
"""
import hashlib
import re

from apps.core.bangla import nfc, search_normalize

from .models import CollectedMessage

KEY_MAX = 512


def content_hash(item, settings):
    text = f"{item.title}\n{item.body_text or item.raw_text}"
    for pattern in settings["dedupe"].get("hash_ignore_patterns") or []:
        text = re.sub(pattern, "", text, flags=re.MULTILINE)
    return hashlib.sha256(" ".join(nfc(text).split()).encode("utf-8")).hexdigest()


def _selector_value(item, selector):
    from bs4 import BeautifulSoup

    html = item.body_html or item.meta.get("_html") or ""
    if not html or not selector:
        return ""
    node = BeautifulSoup(html, "lxml").select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def item_key(item, settings, *, document_url="", digest=""):
    """Stable identity of an item, per the feed's dedupe settings (falls back to the content hash)."""
    dedupe = settings["dedupe"]
    mode = dedupe.get("key_from", "url")
    url = item.source_url or document_url
    if item.source_item_key:
        key = item.source_item_key
    elif mode == "url":
        key = url
    elif mode == "url_title":
        key = f"{url}|{search_normalize(item.title)}" if url or item.title else ""
    elif mode == "title":
        key = search_normalize(item.title)
    elif mode == "date":
        key = item.published_at_text or (item.published_at.isoformat() if item.published_at else "")
    elif mode == "selector":
        key = _selector_value(item, dedupe.get("key_selector", ""))
    elif mode == "regex":
        found = re.search(dedupe.get("key_regex") or "(?!)", item.body_text or item.raw_text, re.MULTILINE)
        key = (found.group(1) if found and found.groups() else found.group(0)) if found else ""
    else:
        key = ""
    key = key or f"hash:{digest}"
    if len(key) > KEY_MAX:
        key = "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    return key


def classify(*, feed, mailbox=None, key, digest, version_on_change=True):
    """Return (decision, existing_message_or_None)."""
    scope = {"feed": feed} if feed is not None else {"feed__isnull": True, "mailbox": mailbox}
    latest = (
        CollectedMessage.objects.filter(source_item_key=key, is_latest=True, **scope)
        .exclude(status="duplicate").order_by("-version", "-id").first()
    )
    if latest is not None:
        if latest.content_hash == digest:
            return "unchanged", latest
        return ("updated" if version_on_change else "changed_ignored"), latest
    same_content = (
        CollectedMessage.objects.filter(content_hash=digest, is_latest=True, **scope)
        .exclude(status__in=["extraction_failed", "duplicate"]).order_by("id").first()
    )
    if same_content is not None:
        return "duplicate", same_content
    return "new", None
