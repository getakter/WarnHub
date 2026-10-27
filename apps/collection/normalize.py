"""Normalize an extracted item: clean NFC text, title fallback, language, legacy fonts, length limits."""
from django.conf import settings as django_settings

from apps.core.bangla import clean_text, detect_language, detect_legacy_bangla

TITLE_MAX = 1000


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def finalize_item(item, document, settings, *, filename="", link_text=""):
    mapping, capture, content = settings["mapping"], settings["capture"], settings["content"]
    for flag in document.quality_flags:
        item.flag(flag)
    for reason in document.attention:
        if reason not in item.attention:
            item.attention.append(reason)

    item.title = " ".join(clean_text(item.title).split())
    item.body_text = clean_text(item.body_text)
    if not item.title:
        fallback = mapping.get("title_fallback", "document_title")
        candidates = {
            "document_title": [document.title, _first_line(item.body_text), item.link_text, filename],
            "first_line": [_first_line(item.body_text), document.title, item.link_text, filename],
            "link_text": [item.link_text, document.title, _first_line(item.body_text), filename],
            "filename": [filename, document.title, _first_line(item.body_text)],
        }[fallback]
        item.title = next((" ".join(c.split()) for c in candidates if c and c.strip()), "")

    legacy, _score, reasons = detect_legacy_bangla(f"{item.title}\n{item.body_text}", document.font_names)
    if legacy:
        item.flag("legacy_bangla_font")
        item.attention.append(
            "Legacy (non-Unicode, Bijoy/SutonnyMJ-style) Bangla detected: " + "; ".join(reasons)
            + ". The garbled text is kept in raw_extracted_text, not stored as clean text."
        )
        item.raw_text = f"{item.title}\n\n{item.body_text}".strip()
        item.body_text = ""
        if detect_legacy_bangla(item.title, document.font_names)[0] or not item.title:
            item.title = filename or "(legacy-font document — needs conversion)"

    language = content.get("language", "auto")
    item.meta["detected_language"] = (
        language if language != "auto" else detect_language(f"{item.title}\n{item.body_text}")
    )

    min_length = capture.get("min_text_length", 0) or 0
    if not legacy and len(item.body_text) < min_length:
        item.flag("empty_body")
        item.attention.append(f"Body text is shorter than {min_length} characters")

    limit = mapping.get("max_body_chars") or django_settings.EWCE_MAX_BODY_CHARS
    if len(item.body_text) > limit:
        item.body_text = item.body_text[:limit]
        item.flag("truncated")
    if len(item.raw_text) > limit:
        item.raw_text = item.raw_text[:limit]
        item.flag("truncated")
    if mapping.get("keep_html"):
        item.body_html = (item.body_html or "")[:limit]
    else:
        item.body_html = ""
    if len(item.title) > TITLE_MAX:
        item.title = item.title[: TITLE_MAX - 1] + "…"
    return item
