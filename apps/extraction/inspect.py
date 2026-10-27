"""Inspect / playground helpers: safe highlighted HTML previews and pattern testing."""
import html as html_lib
import re

import lxml.html

from apps.core.bangla import nfc

from . import select as sel

UNSAFE_TAGS = ["script", "noscript", "iframe", "frame", "object", "embed", "form", "meta[http-equiv]", "link[rel=import]"]
HIGHLIGHT_STYLE = (
    "<style>.ewce-hit{outline:3px solid #dc3545!important;background:rgba(255,235,59,.35)!important}"
    ".ewce-hover{outline:2px dashed #0d6efd!important;cursor:crosshair!important}</style>"
)
MAX_MATCHES = 200


def _neutralize(root):
    sel.remove(root, UNSAFE_TAGS)
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        for attribute in list(node.attrib):
            value = node.attrib[attribute]
            if attribute.lower().startswith("on") or (
                attribute.lower() in {"href", "src", "action"} and value.strip().lower().startswith("javascript:")
            ):
                del node.attrib[attribute]


def safe_preview(root, base_url):
    head = root.find("head")
    if head is None:
        head = lxml.html.Element("head")
        root.insert(0, head)
    head.insert(0, lxml.html.fragment_fromstring(f'<base href="{html_lib.escape(base_url)}">'))
    head.append(lxml.html.fragment_fromstring(HIGHLIGHT_STYLE))
    return lxml.html.tostring(root, encoding="unicode", doctype="<!DOCTYPE html>")


def test_selector(html, base_url, *, css="", xpath=""):
    """Return {"count", "matches": [{"text","html"}], "preview": highlighted page, "error"}."""
    root = sel.parse(html)
    if root is None:
        return {"count": 0, "matches": [], "preview": "", "error": "The page has no HTML to select from."}
    _neutralize(root)
    try:
        nodes = sel.select(root, css=css, xpath=xpath)
    except Exception as exc:  # noqa: BLE001 - invalid expression shown to the admin
        return {"count": 0, "matches": [], "preview": "", "error": f"Invalid {'CSS selector' if css else 'XPath'}: {exc}"}
    matches = []
    for node in nodes[:MAX_MATCHES]:
        if isinstance(node, str):
            matches.append({"text": node.strip()[:500], "html": ""})
            continue
        matches.append({"text": sel.node_text(node)[:500], "html": sel.outer_html(node)[:1500]})
        node.set("class", ((node.get("class") or "") + " ewce-hit").strip())
    return {"count": len(nodes), "matches": matches, "preview": safe_preview(root, base_url), "error": ""}


def test_regex(text, pattern, flags=re.IGNORECASE | re.MULTILINE):
    """Return {"count", "matches": [{"text","groups"}], "highlighted": escaped text with <mark>, "error"}."""
    text = nfc(text or "")
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return {"count": 0, "matches": [], "highlighted": "", "error": f"Invalid regex: {exc}"}
    found = list(compiled.finditer(text))
    out, last = [], 0
    for match in found[:MAX_MATCHES]:
        out.append(html_lib.escape(text[last:match.start()]))
        out.append(f"<mark>{html_lib.escape(match.group(0))}</mark>")
        last = match.end()
    out.append(html_lib.escape(text[last:]))
    return {
        "count": len(found),
        "matches": [{"text": m.group(0)[:500], "groups": {k: v for k, v in (m.groupdict() or {}).items()}
                     or {str(i): g for i, g in enumerate(m.groups(), start=1)}} for m in found[:MAX_MATCHES]],
        "highlighted": "".join(out),
        "error": "",
    }


def preview_page(html, base_url):
    root = sel.parse(html)
    if root is None:
        return ""
    _neutralize(root)
    return safe_preview(root, base_url)


def rule_selector(rule_type, config):
    """The selector a rule's matches can be highlighted with (css, xpath)."""
    if rule_type == "html_selector":
        return config.get("selector", ""), config.get("xpath", "")
    if rule_type == "listing_items":
        return config.get("item_selector", ""), ""
    if rule_type == "section_split" and config.get("split_by") == "headings":
        return ", ".join(config.get("heading_tags") or []), ""
    return "", ""
