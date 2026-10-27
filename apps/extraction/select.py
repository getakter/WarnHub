"""HTML selection helpers (lxml): CSS or XPath, text/HTML/attribute values, clutter removal."""
import re
from urllib.parse import urljoin

import lxml.html
from lxml import etree

from apps.core.bangla import clean_text, nfc

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def parse(html):
    """lxml root for an HTML string (None for empty input)."""
    if not html or not html.strip():
        return None
    try:
        return lxml.html.document_fromstring(_XML_DECL.sub("", html))
    except (etree.ParserError, ValueError):
        return None


def fragment(html):
    root = parse(f"<html><body>{html}</body></html>")
    return root


def remove(root, selectors):
    for selector in selectors or []:
        for node in root.cssselect(selector):
            if node.getparent() is not None:
                node.drop_tree()


def select(root, css="", xpath=""):
    if root is None:
        return []
    if css:
        return root.cssselect(css)
    if xpath:
        result = root.xpath(xpath)
        return result if isinstance(result, list) else [result]
    return [root]


def outer_html(node):
    if isinstance(node, str):
        return node
    return lxml.html.tostring(node, encoding="unicode", with_tail=False)


def inner_html(node):
    if isinstance(node, str):
        return node
    parts = [node.text or ""]
    parts += [lxml.html.tostring(child, encoding="unicode", with_tail=True) for child in node]
    return "".join(parts)


def node_text(node):
    if isinstance(node, str):
        return clean_text(node)
    from .parsers import html_to_text

    return html_to_text(outer_html(node))


def node_value(node, take="text", attribute="", base_url=""):
    if isinstance(node, str):
        return clean_text(node)
    if take == "inner_html":
        return inner_html(node)
    if take == "outer_html":
        return outer_html(node)
    if take == "attribute":
        return (node.get(attribute) or "").strip()
    if take == "href":
        href = (node.get("href") or "").strip()
        return urljoin(base_url, href) if base_url and href else href
    return node_text(node)


def contains_text(node, needle):
    return nfc(needle).casefold() in nfc(node_text(node)).casefold()


def sub_value(row, spec, base_url=""):
    """Listing sub-selector: 'a', 'a@href', '@data-id', '.' (the row itself), 'td:nth-child(2)'."""
    selector, _, attribute = spec.partition("@")
    selector = selector.strip()
    nodes = [row] if selector in ("", ".") else row.cssselect(selector)
    if not nodes:
        return ""
    node = nodes[0]
    if attribute:
        value = (node.get(attribute.strip()) or "").strip()
        if attribute.strip() in {"href", "src"} and value and base_url:
            value = urljoin(base_url, value)
        return value
    return node_text(node)


def suggest_listing_selectors(root, limit=5):
    """Guess repeating-item selectors: parents with many children of the same tag/class."""
    scores = {}
    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        children = [c for c in parent if isinstance(c.tag, str)]
        if len(children) < 3:
            continue
        for child in children:
            classes = ".".join(sorted(c for c in (child.get("class") or "").split() if c))
            key = (_short_path(parent), child.tag, classes)
            scores[key] = scores.get(key, 0) + 1
    ranked = sorted(((n, k) for k, n in scores.items() if n >= 3 and k[1] not in {"script", "style", "option", "br"}),
                    reverse=True)
    out = []
    for count, (parent_path, tag, classes) in ranked[:limit]:
        selector = f"{parent_path} > {tag}{('.' + classes) if classes else ''}"
        out.append({"selector": selector, "count": count})
    return out


def _short_path(node, depth=3):
    parts = []
    while node is not None and isinstance(node.tag, str) and len(parts) < depth:
        if node.get("id") and not re.search(r"\d{3,}", node.get("id")):
            parts.append(f"{node.tag}#{node.get('id')}")
            break
        classes = [c for c in (node.get("class") or "").split() if c and not re.search(r"\d{3,}", c)]
        parts.append(node.tag + ("." + ".".join(classes[:2]) if classes else ""))
        node = node.getparent()
        if node is not None and node.tag in {"html", "body"}:
            break
    return " > ".join(reversed(parts))
