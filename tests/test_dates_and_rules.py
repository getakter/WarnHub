"""Unit tests for date parsing, every rule type and every custom-field extractor."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from apps.extraction.base import Item, ParsedDocument
from apps.extraction.dates import find_dates, parse_date
from apps.extraction.engine import RuleContext
from apps.extraction.fields import ConstantField, CssField, LabelAfterField, MetadataField, RegexField, XPathField
from apps.extraction.rules import (
    DateRule, HtmlSelectorRule, ListingItemsRule, RegexRule, SectionSplitRule, resolve_callable,
)

DHAKA = ZoneInfo("Asia/Dhaka")
SETTINGS = {"content": {"timezone": "Asia/Dhaka"}}


def ctx_for(html="", url="https://bmd.gov.bd/forecast", text=""):
    log = []
    ctx = RuleContext(document=ParsedDocument(file_type="html", html=html, text=text), rule=None, settings=SETTINGS,
                      url=url, file_type="html", log=lambda level, message, **kw: log.append((level, message)))
    ctx.logged = log
    return ctx


def item_for(html="", text="", url="https://bmd.gov.bd/forecast", **kw):
    return Item(body_html=html, body_text=text, source_url=url, **kw)


# --- dates ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("২৭ অক্টোবর ২০২৬", dt.datetime(2026, 10, 27, tzinfo=DHAKA)),
    ("২৭/১০/২০২৬ সকাল ০৯:৩০", dt.datetime(2026, 10, 27, 9, 30, tzinfo=DHAKA)),
    ("২৭/১০/২০২৬ রাত ৯টা", dt.datetime(2026, 10, 27, 21, 0, tzinfo=DHAKA)),
    ("27 October 2026, 3:00 PM", dt.datetime(2026, 10, 27, 15, 0, tzinfo=DHAKA)),
    ("Oct 27, 2026", dt.datetime(2026, 10, 27, tzinfo=DHAKA)),
    ("27th October 2026", dt.datetime(2026, 10, 27, tzinfo=DHAKA)),
    ("2026-10-27 09:30", dt.datetime(2026, 10, 27, 9, 30, tzinfo=DHAKA)),
    ("27.10.2026", dt.datetime(2026, 10, 27, tzinfo=DHAKA)),
    ("D:20261027093000+06'00'", dt.datetime(2026, 10, 27, 9, 30, tzinfo=DHAKA)),
    ("২৭ আগষ্ট ২০২৬", dt.datetime(2026, 8, 27, tzinfo=DHAKA)),
])
def test_parse_bangla_and_english_dates(text, expected):
    assert parse_date(text) == expected


def test_parse_iso_with_offset_and_custom_formats():
    assert parse_date("2026-10-27T03:30:00+00:00") == dt.datetime(2026, 10, 27, 3, 30, tzinfo=dt.timezone.utc)
    assert parse_date("27-Oct-26", formats=["%d-%b-%y"]) == dt.datetime(2026, 10, 27, tzinfo=DHAKA)
    assert parse_date("not a date") is None


def test_find_dates_in_mixed_text_in_order():
    text = "সতর্কবার্তা নং ৫, জারির তারিখ: ২৭ অক্টোবর ২০২৬, পরবর্তী বুলেটিন 28/10/2026 06:00"
    found = find_dates(text)
    assert [d for _t, d in found] == [dt.datetime(2026, 10, 27, tzinfo=DHAKA), dt.datetime(2026, 10, 28, 6, 0, tzinfo=DHAKA)]


# --- html_selector -------------------------------------------------------------------
PAGE = """<html><body><nav>Menu Home</nav><div id="main"><h2>Forecast</h2>
<p class="txt">Heavy rain likely in Sylhet.</p><p class="txt">Signal 3 for Chattogram port.</p>
<a class="pdf" href="/files/b.pdf">Bulletin PDF</a><span data-no="77">No. 77</span></div><footer>Copyright</footer></body></html>"""


def test_html_selector_css_join_removes_clutter():
    rule = HtmlSelectorRule({"selector": "#main", "remove_selectors": ["a.pdf"]})
    items = rule.apply([item_for(PAGE)], ctx_for(PAGE))
    assert "Heavy rain" in items[0].body_text and "Menu" not in items[0].body_text and "Bulletin PDF" not in items[0].body_text
    assert items[0].body_html.startswith("<div")


def test_html_selector_xpath_take_attribute_href_and_targets():
    ctx = ctx_for(PAGE)
    item = item_for(PAGE)
    HtmlSelectorRule({"xpath": "//span[@data-no]", "take": "attribute", "attribute": "data-no", "target": "bulletin_number"}).apply([item], ctx)
    HtmlSelectorRule({"selector": "a.pdf", "take": "href", "target": "pdf_link"}).apply([item], ctx)
    HtmlSelectorRule({"selector": "h2", "target": "title"}).apply([item], ctx)
    assert item.fields == {"bulletin_number": "77", "pdf_link": "https://bmd.gov.bd/files/b.pdf"}
    assert item.title == "Forecast"


def test_html_selector_each_text_contains_and_no_match():
    ctx = ctx_for(PAGE)
    items = HtmlSelectorRule({"selector": "p.txt", "multiple": "each"}).apply([item_for(PAGE)], ctx)
    assert [i.body_text for i in items] == ["Heavy rain likely in Sylhet.", "Signal 3 for Chattogram port."]
    assert items[0].source_item_key != items[1].source_item_key
    items = HtmlSelectorRule({"selector": "p.txt", "text_contains": "signal", "multiple": "first"}).apply([item_for(PAGE)], ctx)
    assert items[0].body_text == "Signal 3 for Chattogram port."
    HtmlSelectorRule({"selector": "div.nothing"}).apply([item_for(PAGE)], ctx)
    assert not ctx.matched and "matched nothing" in ctx.no_match_reason


# --- listing_items -------------------------------------------------------------------
LISTING = """<table><tbody>
<tr><td>১</td><td><a href="/notice/1">বন্যা সতর্কবার্তা</a></td><td>২৭/১০/২০২৬</td></tr>
<tr><td>২</td><td><a href="https://ddm.gov.bd/notice/2">Cyclone advisory</a></td><td>26/10/2026</td></tr>
<tr><td>৩</td><td></td><td>no title row</td></tr>
<tr><td>৪</td><td><span>Untitled link-less notice</span></td><td>25/10/2026</td></tr>
</tbody></table>"""


def test_listing_items_fields_links_and_skips():
    rule = ListingItemsRule({"item_selector": "tbody tr",
                             "fields": {"title": "td:nth-child(2)", "link": "a@href", "date": "td:nth-child(3)",
                                        "serial": "td:nth-child(1)"}})
    ctx = ctx_for(LISTING, url="https://ddm.gov.bd/notices")
    items = rule.apply([item_for(LISTING, url="https://ddm.gov.bd/notices")], ctx)
    assert [i.title for i in items] == ["বন্যা সতর্কবার্তা", "Cyclone advisory", "Untitled link-less notice"]
    assert items[0].source_url == "https://ddm.gov.bd/notice/1" and items[0].published_at_text == "২৭/১০/২০২৬"
    assert items[0].fields == {"serial": "১"}
    assert items[2].source_url == "https://ddm.gov.bd/notices" and items[2].source_item_key.startswith("https://ddm.gov.bd/notices#item:")


def test_listing_items_no_match_keeps_input():
    ctx = ctx_for(LISTING)
    original = [item_for(LISTING)]
    assert ListingItemsRule({"item_selector": "ul li"}).apply(original, ctx) == original
    assert not ctx.matched


# --- section_split -------------------------------------------------------------------
SECTIONED = """<div><p>Issued 27 October 2026</p><h2>ঢাকা বিভাগ</h2><p>হালকা বৃষ্টি হতে পারে এই এলাকায়।</p>
<h2>সিলেট বিভাগ</h2><p>ভারী বৃষ্টি ও বন্যার সম্ভাবনা রয়েছে।</p><h3>Port warning</h3><p>Signal 3 remains hoisted here.</p></div>"""


def test_section_split_by_headings_with_filter_and_preamble():
    ctx = ctx_for(SECTIONED)
    items = SectionSplitRule({"split_by": "headings", "heading_tags": ["h2", "h3"], "min_section_chars": 5}).apply(
        [item_for(SECTIONED, title="Bulletin")], ctx)
    assert [i.title for i in items] == ["ঢাকা বিভাগ", "সিলেট বিভাগ", "Port warning"]
    assert items[1].body_text == "ভারী বৃষ্টি ও বন্যার সম্ভাবনা রয়েছে।"
    assert items[1].fields["section_heading"] == "সিলেট বিভাগ"
    assert len({i.source_item_key for i in items}) == 3
    kept = SectionSplitRule({"split_by": "headings", "heading_tags": ["h2"], "keep_headings_matching": ["সিলেট"],
                             "include_preamble": True}).apply([item_for(SECTIONED, title="Bulletin")], ctx)
    assert [i.title for i in kept] == ["সিলেট বিভাগ"]
    with_preamble = SectionSplitRule({"split_by": "headings", "heading_tags": ["h2"], "include_preamble": True,
                                      "min_section_chars": 5}).apply([item_for(SECTIONED, title="Bulletin")], ctx)
    assert with_preamble[0].body_text == "Issued 27 October 2026" and with_preamble[0].title == "Bulletin"


def test_section_split_regex_and_bold_lines_and_no_match():
    text = "Header line\nWARNING 1: flood\nwater rising at Sirajganj\nWARNING 2: lightning\nstay indoors please"
    ctx = ctx_for(text=text)
    items = SectionSplitRule({"split_by": "regex", "regex": r"^WARNING \d+", "min_section_chars": 5}).apply(
        [item_for(text=text)], ctx)
    assert [i.title for i in items] == ["WARNING 1: flood", "WARNING 2: lightning"]
    bold = "<p><b>Dhaka</b></p><p>light rain expected today</p><p><strong>Khulna</strong></p><p>dry weather expected today</p>"
    items = SectionSplitRule({"split_by": "bold_lines"}).apply([item_for(bold)], ctx_for(bold))
    assert [i.title for i in items] == ["Dhaka", "Khulna"]
    ctx = ctx_for("<p>no headings</p>")
    SectionSplitRule({}).apply([item_for("<p>no headings</p>")], ctx)
    assert not ctx.matched


# --- regex ---------------------------------------------------------------------------
def test_regex_named_groups_map_and_all_as_items():
    text = "Bulletin No: 45\nSignal No. 3 for Mongla\nSignal No. 4 for Payra"
    item = item_for(text=text)
    RegexRule({"pattern": r"Bulletin No:\s*(?P<bulletin>\d+)", "group_map": {"bulletin": "bulletin_number"}}).apply([item], ctx_for())
    assert item.fields["bulletin_number"] == "45"
    items = RegexRule({"pattern": r"Signal No\. (?P<signal>\d) for (?P<title>\w+)", "mode": "all_as_items"}).apply(
        [item_for(text=text)], ctx_for())
    assert [(i.title, i.fields["signal"]) for i in items] == [("Mongla", "3"), ("Payra", "4")]
    ctx = ctx_for()
    RegexRule({"pattern": "zzz"}).apply([item_for(text=text)], ctx)
    assert not ctx.matched


# --- date rule -----------------------------------------------------------------------
def test_date_rule_sources():
    item = item_for(text="জারি: ২৭ অক্টোবর ২০২৬ সকাল ১০:০০\nnext 30/10/2026")
    DateRule({"source": "text"}).apply([item], ctx_for())
    assert item.published_at == dt.datetime(2026, 10, 27, 10, 0, tzinfo=DHAKA)

    html = '<div><span class="date">২৬/১০/২০২৬</span></div>'
    item = item_for(html)
    DateRule({"source": "selector", "selector": "span.date"}).apply([item], ctx_for(html))
    assert item.published_at_text == "২৬/১০/২০২৬" and item.published_at.date() == dt.date(2026, 10, 26)

    item = item_for(published_at_text="25/10/2026")
    DateRule({"source": "field", "field": "date"}).apply([item], ctx_for())
    assert item.published_at.date() == dt.date(2026, 10, 25)

    item = item_for(meta={"pdf.creationDate": "D:20261024080000+06'00'"})
    DateRule({"source": "metadata"}).apply([item], ctx_for())
    assert item.published_at == dt.datetime(2026, 10, 24, 8, 0, tzinfo=DHAKA)

    item = item_for(meta={"filename": "bulletin_20261023.pdf"})
    DateRule({"source": "filename"}).apply([item], ctx_for())
    assert item.published_at.date() == dt.date(2026, 10, 23)

    item = item_for(published_at_text="sometime soon")
    DateRule({"source": "field", "field": "date"}).apply([item], ctx_for())
    assert item.published_at is None and "date_unparsed" in item.quality_flags

    ctx = ctx_for()
    DateRule({"source": "text"}).apply([item_for(text="no dates here")], ctx)
    assert not ctx.matched


def test_custom_callable_restricted_to_plugin_modules():
    with pytest.raises(ValueError, match="not a custom plugin module"):
        resolve_callable("os:system")


# --- custom field extractors ---------------------------------------------------------
def test_field_extractors():
    html = '<div><span class="no">Bulletin 12</span><table><tr><td>Station</td><td>Sylhet</td></tr></table></div>'
    item = item_for(html, text="বুলেটিন নং: ১২\nজারির সময়: সকাল ১০টা\nIssue time - 10:00 BST\n",
                    meta={"filename": "b.pdf", "email.subject": "Flood"})
    ctx = ctx_for(html)
    assert CssField({"selector": "span.no", "post_regex": r"(\d+)"}).extract(item, ctx) == "12"
    assert XPathField({"xpath": "//td[text()='Station']/following-sibling::td"}).extract(item, ctx) == "Sylhet"
    assert RegexField({"pattern": r"বুলেটিন নং[:\s]*(\S+)"}).extract(item, ctx) == "১২"
    assert RegexField({"pattern": r"(?P<t>\d\d:\d\d)", "group": "t"}).extract(item, ctx) == "10:00"
    assert LabelAfterField({"labels": ["Issue time", "জারির সময়"]}).extract(item, ctx) == "10:00 BST"  # list order = priority
    assert LabelAfterField({"labels": ["জারির সময়"]}).extract(item, ctx) == "সকাল ১০টা"
    assert LabelAfterField({"labels": ["Issue time"], "source_field": "body_text"}).extract(
        item_for(text="Issue time - 10:00 BST"), ctx) == "10:00 BST"
    assert MetadataField({"key": "email.subject"}).extract(item, ctx) == "Flood"
    assert MetadataField({"key": "url"}).extract(item, ctx) == "https://bmd.gov.bd/forecast"
    assert ConstantField({"value": "BMD"}).extract(item, ctx) == "BMD"
    assert CssField({"selector": "span.none"}).extract(item, ctx) in ("", None)
