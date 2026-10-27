"""JSON Schemas of the built-in rule types and custom-field extractors (shown as admin forms)."""
from apps.sources.settings_schema import FILE_TYPES

_css = {"type": "string", "format": "css-selector"}
_regex = {"type": "string", "format": "regex"}
_regex_lines = {"type": "array", "items": {"type": "string", "format": "regex"}}
_lines = {"type": "array", "items": {"type": "string"}}
_target = {
    "type": "string", "default": "body_text", "title": "Store result in",
    "description": "title, body_text, published_at_text, source_url, source_item_key or any custom field name",
}

BANGLA_MONTHS = {
    "জানুয়ারি": "January", "জানুয়ারী": "January", "ফেব্রুয়ারি": "February", "ফেব্রুয়ারী": "February",
    "মার্চ": "March", "এপ্রিল": "April", "মে": "May", "জুন": "June", "জুলাই": "July",
    "আগস্ট": "August", "আগষ্ট": "August", "সেপ্টেম্বর": "September", "অক্টোবর": "October",
    "নভেম্বর": "November", "ডিসেম্বর": "December",
    "সকাল": "AM", "দুপুর": "PM", "বিকাল": "PM", "বিকেল": "PM", "সন্ধ্যা": "PM", "রাত": "PM",
    "ঘটিকা": "", "টা": "", "খ্রিস্টাব্দ": "", "খ্রিঃ": "",
}
DEFAULT_DATE_FORMATS = [
    "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d/%m/%Y", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d %B %Y %H:%M", "%d %B %Y", "%d %B, %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
    "%I:%M %p %d %B %Y", "%H:%M %d %B %Y", "%d %B %Y %I:%M %p",
]

HTML_SELECTOR = {
    "type": "object",
    "properties": {
        "selector": {**_css, "title": "Main content CSS selector",
                     "description": "e.g. div.content, #main, article, table.bulletin",
                     "examples": ["div.entry-content", "#content article"]},
        "xpath": {"type": "string", "format": "xpath", "title": "…or XPath",
                  "description": "Used when no CSS selector is given, e.g. //div[@id='main']"},
        "text_contains": {"type": "string", "title": "Only elements whose text contains"},
        "remove_selectors": {"type": "array", "items": _css, "title": "Remove these elements first",
                             "default": ["script", "style", "noscript", "nav", "footer", "header", "iframe", "form"]},
        "take": {"type": "string", "enum": ["text", "inner_html", "outer_html", "attribute", "href"],
                 "default": "text", "title": "Take"},
        "attribute": {"type": "string", "title": "Attribute name (when Take = attribute)"},
        "multiple": {"type": "string", "enum": ["first", "join", "each"], "default": "join",
                     "title": "When several elements match",
                     "x-enum-labels": {"first": "Use the first", "join": "Join them",
                                       "each": "Each match becomes its own message"}},
        "join_with": {"type": "string", "default": "\n\n", "title": "Join with"},
        "target": _target,
    },
}

LISTING_ITEMS = {
    "type": "object",
    "required": ["item_selector"],
    "properties": {
        "item_selector": {**_css, "title": "Repeating item selector",
                          "description": "Each match (table row, card, list entry) becomes one message.",
                          "examples": ["table tbody tr", "ul.notice-list li", "div.card"]},
        "fields": {"type": "object", "additionalProperties": {"type": "string"}, "title": "Sub-selectors per item",
                   "description": "field = selector inside the item. Add @attr for an attribute, e.g. link = a@href. "
                                  "Use . for the item itself. Custom names go to extra fields.",
                   "default": {"title": "a", "link": "a@href"},
                   "examples": [{"title": "td:nth-child(2)", "link": "a@href", "date": "td:nth-child(3)"}]},
        "skip_items_without": {"type": "array", "items": {"type": "string"}, "default": ["title"],
                               "title": "Skip items missing any of these fields"},
        "body_from": {"type": "string", "enum": ["summary", "item_text"], "default": "item_text",
                      "title": "Body text", "x-enum-labels": {"summary": "The 'summary' field",
                                                             "item_text": "Whole item text"}},
    },
}

SECTION_SPLIT = {
    "type": "object",
    "properties": {
        "split_by": {"type": "string", "enum": ["headings", "bold_lines", "regex"], "default": "headings",
                     "title": "Split on", "x-enum-labels": {"headings": "Heading tags (HTML)",
                                                           "bold_lines": "Bold lines (HTML <b>/<strong>)",
                                                           "regex": "Lines matching a pattern (any text)"}},
        "heading_tags": {"type": "array", "items": {"type": "string", "enum": ["h1", "h2", "h3", "h4", "h5", "h6"]},
                         "default": ["h2", "h3"], "title": "Heading tags"},
        "regex": {**_regex, "title": "Section start pattern",
                  "description": "A line matching this starts a new section, e.g. ^(সতর্কবার্তা|Warning)\\b"},
        "keep_headings_matching": {**_lines, "title": "Keep only sections whose heading contains",
                                   "description": "Blank = keep all sections."},
        "include_preamble": {"type": "boolean", "default": False, "title": "Keep text before the first heading"},
        "heading_as_title": {"type": "boolean", "default": True, "title": "Use heading as message title"},
        "min_section_chars": {"type": "integer", "minimum": 0, "default": 20, "title": "Drop sections shorter than"},
    },
}

REGEX_RULE = {
    "type": "object",
    "required": ["pattern"],
    "properties": {
        "pattern": {**_regex, "title": "Pattern with named groups",
                    "description": "(?P<field>...) groups become fields, e.g. Bulletin No[.:]\\s*(?P<bulletin_number>\\S+)"},
        "source_field": {"type": "string", "enum": ["body_text", "title", "body_html", "source_url"],
                         "default": "body_text", "title": "Search in"},
        "flags": {"type": "array", "items": {"type": "string", "enum": ["IGNORECASE", "MULTILINE", "DOTALL"]},
                  "default": ["IGNORECASE", "MULTILINE"], "title": "Flags"},
        "mode": {"type": "string", "enum": ["first", "all_as_items"], "default": "first", "title": "Mode",
                 "x-enum-labels": {"first": "First match sets fields", "all_as_items": "Every match becomes a message"}},
        "group_map": {"type": "object", "additionalProperties": {"type": "string"}, "title": "Rename groups",
                      "description": "group = field, e.g. heading = title"},
    },
}

JSON_FIELDS = {
    "type": "object",
    "required": ["fields"],
    "properties": {
        "items_path": {"type": "string", "title": "Path to the item(s)",
                       "description": "Dotted path from the top of the JSON; list indexes are numbers. A list makes "
                                      "one message per entry. Blank = the whole document.",
                       "examples": ["data.Scroll", "data.items", "results.0"]},
        "fields": {"type": "object", "additionalProperties": {"type": "string"}, "title": "Fields per item",
                   "description": "field = dotted path inside the item. title, summary (body), body_html, link, "
                                  "date are core fields; other names go to extra fields.",
                   "default": {"title": "title", "summary": "body"},
                   "examples": [{"body_html": "setvalue", "date": "timeUpdate"}]},
        "strip_html": {"type": "boolean", "default": True, "title": "Convert HTML values to plain text",
                       "description": "For title / summary and extra fields (body_html always keeps the HTML)."},
        "skip_items_without": {"type": "array", "items": {"type": "string"}, "default": [],
                               "title": "Skip items missing any of these fields"},
    },
}

PDF_RULE = {
    "type": "object",
    "properties": {
        "page_range": {"type": "string", "title": "Pages", "description": "Blank = all. e.g. 1-3,5 or -2 (last two)",
                       "pattern": r"^\s*(-?\d+(\s*-\s*\d+)?\s*(,\s*-?\d+(\s*-\s*\d+)?\s*)*)?$"},
        "text_mode": {"type": "string", "enum": ["text_layer", "text_then_ocr", "ocr"], "default": "text_then_ocr",
                      "title": "Text source", "x-enum-labels": {"text_layer": "Text layer only",
                                                                "text_then_ocr": "Text layer; OCR pages without text",
                                                                "ocr": "Always OCR"}},
        "layout": {"type": "string", "enum": ["plain", "preserve_layout", "tables_as_text"], "default": "plain",
                   "title": "Columns & tables"},
        "remove_line_patterns": {**_regex_lines, "title": "Remove header/footer lines matching"},
        "remove_repeating_lines": {"type": "boolean", "default": True,
                                   "title": "Remove lines repeated on most pages (headers/footers)"},
        "min_chars_per_page": {"type": "integer", "minimum": 0, "default": 30,
                               "title": "Treat page as scanned below N characters"},
    },
}

OFFICE_RULE = {
    "type": "object",
    "properties": {
        "docx_include": {"type": "string", "enum": ["both", "paragraphs", "tables"], "default": "both",
                         "title": "Word: include"},
        "sheets": {**_lines, "title": "Excel: sheets (names or 1-based numbers)", "description": "Blank = all"},
        "header_row": {"type": "integer", "minimum": 0, "default": 1, "title": "Header row (0 = none)"},
        "row_range": {"type": "string", "pattern": r"^(\d+\s*-\s*\d*)?$", "title": "Rows, e.g. 2-200"},
        "column_range": {"type": "string", "pattern": r"^([A-Za-z]+\s*-\s*[A-Za-z]+)?$", "title": "Columns, e.g. A-F"},
        "row_format": {"type": "string", "enum": ["key_value", "tab"], "default": "key_value",
                       "title": "Row to text", "x-enum-labels": {"key_value": "Header: value; …", "tab": "Tab separated"}},
        "rows_as_items": {"type": "boolean", "default": False, "title": "Each row becomes a message"},
    },
}

EMAIL_RULE = {
    "type": "object",
    "properties": {
        "body_source": {"type": "string", "enum": ["prefer_plain", "prefer_html", "plain", "html"],
                        "default": "prefer_plain", "title": "Body source"},
        "strip_quoted": {"type": "boolean", "default": True, "title": "Strip quoted replies (> lines, 'On … wrote:')"},
        "signature_patterns": {**_regex_lines, "title": "Signature starts at a line matching",
                               "default": [r"^--\s*$", r"^Sent from my ", r"^(Best )?[Rr]egards,?\s*$",
                                           r"^ধন্যবাদান্তে", r"^শুভেচ্ছান্তে"]},
        "remove_line_patterns": {**_regex_lines, "title": "Remove lines matching (disclaimers etc.)"},
    },
}

DATE_RULE = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["text", "selector", "regex", "metadata", "filename", "field"],
                   "default": "text", "title": "Find the date in",
                   "x-enum-labels": {"text": "Body/title text (first date found)", "selector": "A CSS selector",
                                     "regex": "A regex (group 1 or 'date')", "metadata": "Document metadata / meta tags",
                                     "filename": "File name / URL", "field": "An already extracted field"}},
        "selector": {**_css, "title": "Date selector"},
        "regex": {**_regex, "title": "Date regex", "examples": [r"Date[:\s]+(?P<date>[\d/.-]+)"]},
        "field": {"type": "string", "default": "date", "title": "Field name (source = field)"},
        "metadata_keys": {**_lines, "title": "Metadata keys",
                          "default": ["article:published_time", "date", "DC.date", "pubdate", "creationDate",
                                      "Date", "published"]},
        "formats": {**_lines, "title": "Date formats (Python strptime, tried in order)", "default": DEFAULT_DATE_FORMATS},
        "month_aliases": {"type": "object", "additionalProperties": {"type": "string"},
                          "title": "Word replacements before parsing (Bangla months, AM/PM words)",
                          "default": BANGLA_MONTHS},
        "keep_text_if_unparsed": {"type": "boolean", "default": True,
                                  "title": "Keep the raw date text when it can't be parsed (flag date_unparsed)"},
    },
}

CUSTOM_PLUGIN_RULE = {
    "type": "object",
    "required": ["callable"],
    "properties": {
        "callable": {"type": "string", "pattern": r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$", "title": "Function",
                     "description": "module:function inside custom_plugins/, e.g. ewce_custom_plugins.my_site:extract. "
                                    "Called as function(items, ctx, options) and must return a list of items."},
        "options": {"type": "object", "x-widget": "json", "title": "Options passed to the function", "default": {}},
    },
}

FIELD_CSS = {
    "type": "object", "required": ["selector"],
    "properties": {
        "selector": {**_css, "title": "CSS selector"},
        "take": {"type": "string", "enum": ["text", "attribute", "inner_html"], "default": "text"},
        "attribute": {"type": "string", "title": "Attribute"},
        "join_with": {"type": "string", "default": " ", "title": "Join multiple matches with"},
        "post_regex": {**_regex, "title": "Then keep regex group 1 of the value"},
    },
}
FIELD_XPATH = {
    "type": "object", "required": ["xpath"],
    "properties": {
        "xpath": {"type": "string", "format": "xpath", "title": "XPath"},
        "post_regex": {**_regex, "title": "Then keep regex group 1"},
    },
}
FIELD_REGEX = {
    "type": "object", "required": ["pattern"],
    "properties": {
        "pattern": {**_regex, "title": "Pattern", "examples": [r"(?:Bulletin|বুলেটিন)\s*(?:No|নং)[.:\s]*(\S+)"]},
        "group": {"type": "string", "default": "1", "title": "Group (number or name)"},
        "source_field": {"type": "string", "default": "body_text", "title": "Search in field"},
        "ignore_case": {"type": "boolean", "default": True},
    },
}
FIELD_LABEL_AFTER = {
    "type": "object", "required": ["labels"],
    "properties": {
        "labels": {**_lines, "title": "Label text (any of)", "examples": [["Issue time", "জারির সময়"]]},
        "stop_at": {**_regex, "default": r"\n", "title": "Value ends at (regex)"},
        "max_chars": {"type": "integer", "minimum": 1, "default": 200},
        "strip_chars": {"type": "string", "default": ":：-–— \t", "title": "Strip these characters around the value"},
        "source_field": {"type": "string", "default": "body_text"},
    },
}
FIELD_METADATA = {
    "type": "object", "required": ["key"],
    "properties": {
        "key": {"type": "string", "title": "Metadata key",
                "description": "e.g. filename, url, link_text, http.last-modified, email.subject, email.from, pdf.title"},
    },
}
FIELD_CONSTANT = {"type": "object", "required": ["value"], "properties": {"value": {"type": "string", "title": "Value"}}}

FILE_TYPE_ENUM = FILE_TYPES
