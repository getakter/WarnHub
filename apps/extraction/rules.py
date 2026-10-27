"""Built-in extraction rule types (see engine.py for the plugin contract)."""
import copy
import importlib
import json
import re
from urllib.parse import urljoin

from apps.core.bangla import clean_text, nfc, search_normalize
from apps.plugins.registry import register

from . import rule_schemas as S
from . import select as sel
from .base import ExtractorPlugin, Item
from .dates import find_dates, parse_date

CORE_LISTING_FIELDS = {"title": "title", "link": "source_url", "url": "source_url", "date": "published_at_text",
                       "summary": "body_text"}
ALLOWED_CALLABLE_PREFIXES = ("ewce_custom_plugins.",)


def _item_html(item, ctx):
    return item.body_html or ""


def _clone(item, **changes):
    new = copy.deepcopy(item)
    for key, value in changes.items():
        setattr(new, key, value)
    return new


def _key_suffix(text):
    return search_normalize(text)[:120] or "item"


def resolve_callable(path):
    """'module:function' restricted to custom plugin modules (and EWCE_PLUGIN_MODULES)."""
    from django.conf import settings

    module_name, _, function_name = path.partition(":")
    allowed = ALLOWED_CALLABLE_PREFIXES + tuple(f"{m}" for m in settings.EWCE_PLUGIN_MODULES)
    if not module_name.startswith(allowed) and module_name not in settings.EWCE_PLUGIN_MODULES:
        raise ValueError(f"{module_name!r} is not a custom plugin module (allowed: custom_plugins/*.py → "
                         f"ewce_custom_plugins.<file>, or modules listed in EWCE_PLUGIN_MODULES)")
    module = importlib.import_module(module_name)
    return getattr(module, function_name)


@register
class HtmlSelectorRule(ExtractorPlugin):
    name = "html_selector"
    label = "HTML selector extraction"
    description = "Pick the main content (or any value) with a CSS selector or XPath, after removing clutter."
    config_schema = S.HTML_SELECTOR

    def apply(self, items, ctx):
        cfg = self.config
        out, matched = [], False
        for item in items:
            root = sel.parse(_item_html(item, ctx))
            if root is None:
                out.append(item)
                continue
            sel.remove(root, cfg.get("remove_selectors"))
            nodes = sel.select(root, cfg.get("selector", ""), cfg.get("xpath", ""))
            if cfg.get("text_contains"):
                nodes = [n for n in nodes if sel.contains_text(n, cfg["text_contains"])]
            if not nodes:
                out.append(item)
                continue
            matched = True
            take, target = cfg["take"], cfg.get("target") or "body_text"
            values = [sel.node_value(n, take, cfg.get("attribute", ""), ctx.url) for n in nodes]
            if cfg["multiple"] == "each":
                for index, (node, value) in enumerate(zip(nodes, values)):
                    new = _clone(item)
                    self._set(new, target, value, take, node)
                    new.source_item_key = item.source_item_key or f"{item.source_url}#{index + 1}:{_key_suffix(sel.node_text(node)[:80])}"
                    out.append(new)
                continue
            if cfg["multiple"] == "first":
                nodes, values = nodes[:1], values[:1]
            joined = cfg.get("join_with", "\n\n").join(v for v in values if v)
            self._set(item, target, joined, take, nodes[0] if len(nodes) == 1 else None, nodes)
            out.append(item)
        if not matched:
            ctx.no_match(f"selector {cfg.get('selector') or cfg.get('xpath')!r} matched nothing")
        return out

    @staticmethod
    def _set(item, target, value, take, node=None, nodes=None):
        if target == "body_text":
            if take in {"inner_html", "outer_html"}:
                from .parsers import html_to_text

                item.body_html = value
                item.body_text = html_to_text(value)
            else:
                item.body_text = value
                html_nodes = [node] if node is not None else (nodes or [])
                item.body_html = "".join(sel.outer_html(n) for n in html_nodes if not isinstance(n, str))
        elif target == "body_html":
            item.body_html = value
        else:
            item.set(target, clean_text(value) if take == "text" else value)


@register
class ListingItemsRule(ExtractorPlugin):
    name = "listing_items"
    label = "Listing item extraction"
    description = "Each repeating row/card/list entry becomes one message, with sub-selectors for its fields."
    config_schema = S.LISTING_ITEMS
    produces_items = True

    def apply(self, items, ctx):
        cfg = self.config
        out = []
        for item in items:
            root = sel.parse(_item_html(item, ctx))
            if root is None:
                continue
            for row in root.cssselect(cfg["item_selector"]):
                values = {name: sel.sub_value(row, spec, ctx.url) for name, spec in (cfg.get("fields") or {}).items()}
                if any(not values.get(name) for name in cfg.get("skip_items_without") or []):
                    continue
                new = Item(meta=dict(item.meta), body_html=sel.outer_html(row), source_url=item.source_url,
                           published_at=item.published_at, quality_flags=list(item.quality_flags),
                           attention=list(item.attention))
                for name, value in values.items():
                    if not value:
                        continue
                    target = CORE_LISTING_FIELDS.get(name, name)
                    if target == "source_url" and value:
                        value = urljoin(ctx.url, value) if ctx.url else value
                    new.set(target, value)
                if cfg.get("body_from") == "item_text" or not values.get("summary"):
                    new.body_text = sel.node_text(row)
                new.link_text = new.title
                if not values.get("link") and not values.get("url"):
                    new.source_item_key = f"{item.source_url}#item:{_key_suffix(new.title or new.body_text[:80])}"
                out.append(new)
        if not out:
            ctx.no_match(f"no items matched {cfg['item_selector']!r}")
            return items
        return out


_HTML_TAG = re.compile(r"<[a-zA-Z/!][^>]*>|&[a-zA-Z]+;|&#\d+;")


def json_path(data, path):
    """'data.Scroll.0.setvalue' -> value, or None when any step is missing."""
    node = data
    for part in (path or "").split("."):
        if part == "":
            continue
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.lstrip("-").isdigit() and -len(node) <= int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


@register
class JsonFieldsRule(ExtractorPlugin):
    name = "json_fields"
    label = "JSON field extraction"
    description = "Pick values from a JSON response by dotted path; each entry of a list can become a message."
    config_schema = S.JSON_FIELDS
    produces_items = True

    def apply(self, items, ctx):
        from .parsers import html_to_text

        cfg = self.config
        if ctx.document.data is None:
            ctx.no_match("the document is not JSON")
            return items
        found = json_path(ctx.document.data, cfg.get("items_path", ""))
        entries = found if isinstance(found, list) else [] if found is None else [found]
        base = items[0] if items else Item(source_url=ctx.url)
        out = []
        for index, entry in enumerate(entries, start=1):
            values = {}
            for name, path in (cfg.get("fields") or {}).items():
                value = json_path(entry, path)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                values[name] = "" if value is None else str(value).strip()
            if any(not values.get(name) for name in cfg.get("skip_items_without") or []):
                continue
            new = Item(meta=dict(base.meta), source_url=base.source_url, link_text=base.link_text,
                       quality_flags=list(base.quality_flags), attention=list(base.attention))
            for name, value in values.items():
                if not value:
                    continue
                if name == "body_html":
                    new.body_html = value
                    if not values.get("summary"):
                        new.body_text = html_to_text(value)
                    continue
                if cfg.get("strip_html", True) and _HTML_TAG.search(value):
                    value = html_to_text(value)
                target = CORE_LISTING_FIELDS.get(name, name)
                if target == "source_url":
                    value = urljoin(ctx.url, value) if ctx.url else value
                new.set(target, value)
            if len(entries) > 1 and not values.get("link") and not values.get("url"):
                new.source_item_key = f"{base.source_url}#item:{_key_suffix(new.title or new.body_text[:80]) or index}"
            out.append(new)
        if not out:
            ctx.no_match(f"no items at {cfg.get('items_path') or '(top level)'!r}")
            return items
        return out


@register
class SectionSplitRule(ExtractorPlugin):
    name = "section_split"
    label = "Section / heading splitting"
    description = "Split one page or document into several messages by headings, bold lines or text patterns."
    config_schema = S.SECTION_SPLIT
    produces_items = True
    MARK = "⁣§EWCE-H§"

    def apply(self, items, ctx):
        out, split_any = [], False
        for item in items:
            sections = self._sections(item)
            if sections is None:
                out.append(item)
                continue
            split_any = True
            out.extend(self._items_from(item, sections))
        if not split_any:
            ctx.no_match("no section headings found")
        return out

    def _sections(self, item):
        cfg = self.config
        if cfg["split_by"] == "regex":
            if not cfg.get("regex"):
                return None
            pattern = re.compile(cfg["regex"], re.IGNORECASE | re.MULTILINE)
            lines = item.body_text.splitlines()
            marked = [f"{self.MARK}{line}" if pattern.search(line) else line for line in lines]
        else:
            root = sel.parse(item.body_html)
            if root is None:
                return None
            if cfg["split_by"] == "headings":
                heads = [n for tag in cfg["heading_tags"] for n in root.iter(tag)]
            else:
                heads = [n for n in root.iter("b", "strong")
                         if n.getparent() is not None and sel.node_text(n.getparent()) == sel.node_text(n)]
            if not heads:
                return None
            for node in heads:
                node.text = self.MARK + (node.text or "")
            from .parsers import html_to_text

            marked = html_to_text(sel.outer_html(root)).splitlines()
        sections, heading, body = [], None, []
        for line in marked:
            if self.MARK in line:
                if heading is not None or body:
                    sections.append((heading, body))
                heading, body = line.replace(self.MARK, "").strip(), []
            else:
                body.append(line)
        sections.append((heading, body))
        if not any(h is not None for h, _ in sections):
            return None
        return sections

    def _items_from(self, item, sections):
        cfg = self.config
        keep = [nfc(k).casefold() for k in cfg.get("keep_headings_matching") or []]
        out = []
        for heading, body_lines in sections:
            body = clean_text("\n".join(body_lines))
            if heading is None:
                if not cfg.get("include_preamble"):
                    continue
                heading = ""
            if keep and not any(k in nfc(heading).casefold() for k in keep):
                continue
            if len(body) < cfg.get("min_section_chars", 0):
                continue
            new = _clone(item, body_text=body, body_html="")
            new.fields["section_heading"] = heading
            if cfg.get("heading_as_title") and heading:
                new.title = heading
            base = item.source_item_key or item.source_url
            new.source_item_key = f"{base}#section:{_key_suffix(heading or 'preamble')}"
            out.append(new)
        return out


@register
class RegexRule(ExtractorPlugin):
    name = "regex"
    label = "Regex extraction"
    description = "A pattern whose named groups become fields (or one message per match)."
    config_schema = S.REGEX_RULE

    def apply(self, items, ctx):
        cfg = self.config
        flags = 0
        for name in cfg.get("flags") or []:
            flags |= getattr(re, name)
        pattern = re.compile(cfg["pattern"], flags)
        mapping = cfg.get("group_map") or {}
        out, matched = [], False
        for item in items:
            text = nfc(item.get(cfg["source_field"]) or "")
            if cfg["mode"] == "all_as_items":
                matches = list(pattern.finditer(text))
                if not matches:
                    out.append(item)
                    continue
                matched = True
                for index, match in enumerate(matches, start=1):
                    new = _clone(item, body_text=match.group(0).strip(), body_html="")
                    self._assign(new, match, mapping)
                    new.source_item_key = f"{item.source_item_key or item.source_url}#match:{_key_suffix(new.title or match.group(0)[:80]) or index}"
                    out.append(new)
            else:
                match = pattern.search(text)
                if match:
                    matched = True
                    self._assign(item, match, mapping)
                out.append(item)
        if not matched:
            ctx.no_match("pattern did not match")
        return out

    @staticmethod
    def _assign(item, match, mapping):
        for group, value in match.groupdict().items():
            if value is not None:
                target = mapping.get(group, group)
                item.set(target, clean_text(value))


@register
class PdfRule(ExtractorPlugin):
    name = "pdf"
    label = "PDF options"
    description = "Page range, text layer vs OCR, tables/columns, header/footer removal."
    config_schema = S.PDF_RULE
    phase = "parse"


@register
class OfficeRule(ExtractorPlugin):
    name = "office"
    label = "Office file options (Word / Excel)"
    description = "Word paragraphs/tables; Excel sheets, row/column ranges, header row, rows to text."
    config_schema = S.OFFICE_RULE
    phase = "parse"


@register
class EmailRule(ExtractorPlugin):
    name = "email"
    label = "Email body options"
    description = "Plain vs HTML body, quoted-reply and signature stripping."
    config_schema = S.EMAIL_RULE
    phase = "parse"


@register
class DateRule(ExtractorPlugin):
    name = "date"
    label = "Date extraction"
    description = "Find the published date (selector/regex/metadata/filename) and parse Bangla or English formats."
    config_schema = S.DATE_RULE
    sets_extracting_rule = False

    def apply(self, items, ctx):
        cfg = self.config
        tz = (ctx.settings or {}).get("content", {}).get("timezone", "Asia/Dhaka")
        found_any = False
        for item in items:
            if item.published_at is not None:
                found_any = True
                continue
            raw, parsed = self._find(item, ctx, cfg, tz)
            if raw:
                found_any = True
                item.published_at_text = raw[:300]
                item.published_at = parsed
                if parsed is None and cfg.get("keep_text_if_unparsed", True):
                    item.flag("date_unparsed")
        if not found_any:
            ctx.no_match(f"no date found (source: {cfg['source']})")
        return items

    def _find(self, item, ctx, cfg, tz):
        formats, aliases = cfg.get("formats"), cfg.get("month_aliases")
        source = cfg["source"]
        if source == "text":
            hits = find_dates(f"{item.title}\n{item.body_text}", formats, aliases, tz)
            return hits[0] if hits else (None, None)
        if source == "field":
            raw = item.published_at_text if cfg.get("field") in ("date", "published_at_text") and item.published_at_text \
                else str(item.fields.get(cfg.get("field", "date"), "") or item.published_at_text)
        elif source == "selector":
            root = sel.parse(item.body_html or ctx.document.html)
            nodes = sel.select(root, cfg.get("selector", "")) if root is not None and cfg.get("selector") else []
            raw = sel.node_text(nodes[0]) if nodes else ""
            if raw and not nodes[0].text_content().strip():
                raw = nodes[0].get("datetime", "") or nodes[0].get("content", "")
        elif source == "regex":
            match = re.search(cfg.get("regex") or "(?!)", f"{item.title}\n{item.body_text}", re.IGNORECASE)
            raw = (match.groupdict().get("date") or (match.group(1) if match.groups() else match.group(0))) if match else ""
        elif source == "metadata":
            raw = ""
            for key in cfg.get("metadata_keys") or []:
                for candidate in (key, f"pdf.{key}", f"docx.{key}", f"email.{key}", f"http.{key.lower()}"):
                    if item.meta.get(candidate):
                        raw = str(item.meta[candidate])
                        break
                if raw:
                    break
        elif source == "filename":
            raw = item.meta.get("filename") or item.meta.get("url") or item.source_url
            hits = find_dates(re.sub(r"[_]", " ", raw), formats, aliases, tz)
            if not hits:
                compact = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", raw)
                if compact:
                    return compact.group(0), parse_date("-".join(compact.groups()), ["%Y-%m-%d"], {}, tz)
            return hits[0] if hits else (None, None)
        else:
            raw = ""
        raw = clean_text(raw)
        if not raw:
            return None, None
        parsed = parse_date(raw, formats, aliases, tz)
        if parsed is None:
            hits = find_dates(raw, formats, aliases, tz)
            parsed = hits[0][1] if hits else None
        return raw, parsed


@register
class CustomCallableRule(ExtractorPlugin):
    name = "custom_plugin"
    label = "Custom Python function"
    description = "Call a function from custom_plugins/ for sources that configuration cannot handle."
    config_schema = S.CUSTOM_PLUGIN_RULE

    def apply(self, items, ctx):
        function = resolve_callable(self.config["callable"])
        return function(items, ctx, self.config.get("options") or {})
