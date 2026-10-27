"""Acquisition methods end to end with mocked HTTP (responses)."""
import datetime as dt

import pytest
import responses

from apps.acquisition.crawl import discover_links, expand_entry_url, in_scope, next_page_url
from apps.acquisition.http import FetchError, PoliteClient, clear_robots_cache
from apps.collection.models import CollectedMessage
from apps.collection.pipeline import run_feed
from apps.sources.models import CustomFieldDefinition, ExtractionRule
from tests.helpers import make_docx, make_pdf

FAST = {"per_domain_delay_seconds": 0, "retries": 0, "respect_robots": False}


@pytest.fixture(autouse=True)
def _clean_robots():
    clear_robots_cache()
    yield
    clear_robots_cache()


@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def _configure(feed, method, urls, settings=None, method_config=None):
    feed.method = method
    feed.entry_urls = urls
    feed.method_config = method_config or {}
    feed.settings = {**feed.settings, "fetch": {**FAST, **(settings or {}).pop("fetch", {})}, **(settings or {})}
    feed.save()
    return feed


# --- crawl helpers -------------------------------------------------------------------
def test_expand_entry_url_uses_dhaka_date():
    now = dt.datetime(2026, 10, 27, 20, 0, tzinfo=dt.timezone.utc)  # already 28 Oct in Dhaka
    assert expand_entry_url("https://x/{YYYY}/{MM}/{DD}/b-{D}-{M}-{YY}.pdf", now=now) == "https://x/2026/10/28/b-28-10-26.pdf"


def test_in_scope_rules():
    scope = {"same_domain_only": True, "include_patterns": ["*.pdf", "*/bulletin/*"], "exclude_patterns": ["*archive*"],
             "link_text_exclude": ["English version"]}
    base = "https://www.ffwc.gov.bd/notices"
    assert in_scope("https://ffwc.gov.bd/files/a.pdf?dl=1", "Bulletin", scope, base)[0]
    assert in_scope("https://www.ffwc.gov.bd/bulletin/today", "x", scope, base)[0]
    assert in_scope("https://other.org/a.pdf", "x", scope, base) == (False, "other domain")
    assert in_scope("https://ffwc.gov.bd/archive/a.pdf", "x", scope, base)[1] == "matches an exclude pattern"
    assert in_scope("https://ffwc.gov.bd/page.html", "x", scope, base)[1] == "not matching include patterns"
    assert in_scope("https://ffwc.gov.bd/a.pdf", "English version", scope, base)[1] == "link text excluded"
    assert in_scope("https://ffwc.gov.bd/x/a.pdf", "x", {"include_patterns": [r"/x/.*\.pdf$"], "pattern_syntax": "regex"}, base)[0]


def test_discover_links_with_items_and_next_page():
    html = """<ul><li class="n"><a href="a.pdf">বন্যা বুলেটিন</a><span class="d">27/10/2026</span></li>
              <li class="n"><a href="/b.pdf"></a>Cyclone notice<span class="d">26/10/2026</span></li></ul>
              <a href="#top">top</a><a href="javascript:void(0)">x</a><a class="next" href="?page=2">Next</a>"""
    links = discover_links(html, "https://ddm.gov.bd/n/", {}, item_selector="li.n", item_date_selector="span.d")
    assert links == [
        {"url": "https://ddm.gov.bd/n/a.pdf", "text": "বন্যা বুলেটিন", "date_text": "27/10/2026"},
        {"url": "https://ddm.gov.bd/b.pdf", "text": "Cyclone notice 26/10/2026", "date_text": "26/10/2026"},
    ]
    assert next_page_url(html, "https://ddm.gov.bd/n/", "a.next") == "https://ddm.gov.bd/n/?page=2"


# --- HTTP client ---------------------------------------------------------------------
def test_client_identifies_itself_and_enforces_limits(http, settings):
    http.get("https://bmd.gov.bd/a", body="ok", headers={"Content-Type": "text/html"})
    http.get("https://bmd.gov.bd/big", body=b"x" * 3000)
    http.get("https://bmd.gov.bd/missing", status=404)
    client = PoliteClient({**FAST, "max_download_mb": 0.002, "headers": {"X-Test": "1"}})
    result = client.get("https://bmd.gov.bd/a")
    assert result.content == b"ok" and http.calls[0].request.headers["User-Agent"] == settings.EWCE_USER_AGENT
    assert http.calls[0].request.headers["X-Test"] == "1"
    with pytest.raises(FetchError, match="download limit"):
        client.get("https://bmd.gov.bd/big")
    with pytest.raises(FetchError, match="HTTP 404"):
        client.get("https://bmd.gov.bd/missing")


def test_robots_txt_respected(http):
    http.get("https://ffwc.gov.bd/robots.txt", body="User-agent: *\nDisallow: /private/\n")
    http.get("https://ffwc.gov.bd/public", body="ok")
    client = PoliteClient({**FAST, "respect_robots": True})
    assert client.get("https://ffwc.gov.bd/public").content == b"ok"
    with pytest.raises(FetchError, match="robots.txt disallows"):
        client.get("https://ffwc.gov.bd/private/x")


def test_basic_auth_from_env_names(http, monkeypatch):
    monkeypatch.setenv("EWCE_TEST_USER", "u")
    monkeypatch.setenv("EWCE_TEST_PASS", "p")
    http.get("https://x.gov.bd/secure", body="ok")
    PoliteClient({**FAST, "auth_user_env": "EWCE_TEST_USER", "auth_password_env": "EWCE_TEST_PASS"}).get("https://x.gov.bd/secure")
    assert http.calls[0].request.headers["Authorization"].startswith("Basic ")
    with pytest.raises(FetchError, match="env vars not set"):
        PoliteClient({"auth_user_env": "EWCE_NOPE", "auth_password_env": "EWCE_NOPE2"})


# --- web_page ------------------------------------------------------------------------
BMD_PAGE = """<html><head><title>BMD Forecast</title></head><body><nav>menu</nav>
<div id="forecast"><h1>আবহাওয়ার পূর্বাভাস</h1><p>বুলেটিন নং: ১২</p>
<p>জারির তারিখ: ২৭ অক্টোবর ২০২৬ সকাল ০৯:০০</p><p>সিলেট বিভাগে ভারী বৃষ্টি হতে পারে।</p></div></body></html>"""


@pytest.mark.django_db
def test_web_page_feed_end_to_end_with_versions(feed, http):
    _configure(feed, "web_page", ["https://bmd.gov.bd/forecast"])
    ExtractionRule.objects.create(feed=feed, name="main", position=10, rule_type="html_selector",
                                  config={"selector": "#forecast"})
    ExtractionRule.objects.create(feed=feed, name="title", position=20, rule_type="html_selector",
                                  config={"selector": "#forecast h1", "target": "title"})
    ExtractionRule.objects.create(feed=feed, name="date", position=30, rule_type="date", config={"source": "text"})
    CustomFieldDefinition.objects.create(feed=feed, name="bulletin_number", extractor="label_after",
                                         config={"labels": ["বুলেটিন নং"]})
    http.get("https://bmd.gov.bd/forecast", body=BMD_PAGE.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"})
    ctx = run_feed(feed)
    assert ctx.status == "success", ctx.events
    message = CollectedMessage.objects.get()
    assert message.title == "আবহাওয়ার পূর্বাভাস"
    assert "menu" not in message.body_text and "ভারী বৃষ্টি" in message.body_text
    assert message.extra_fields == {"bulletin_number": "১২"}
    assert message.published_at == dt.datetime(2026, 10, 27, 3, 0, tzinfo=dt.timezone.utc)
    assert message.extracting_rule.name == "title" and message.raw_artifact.file_type == "html"

    run_feed(feed)
    assert CollectedMessage.objects.count() == 1
    http.replace(responses.GET, "https://bmd.gov.bd/forecast", body=BMD_PAGE.replace("ভারী", "অতি ভারী").encode("utf-8"),
                 headers={"Content-Type": "text/html; charset=utf-8"})
    run_feed(feed)
    assert list(CollectedMessage.objects.order_by("version").values_list("version", "status", "is_latest")) == [
        (1, "new", False), (2, "updated", True)]


BMD_SCROLL = {"data": {"Slides": [{"setvalue": "file/a.jpg"}], "Scroll": [{
    "id": "1", "setkey": "scrollTop", "timeUpdate": "2026-10-27 10:41:59",
    "setvalue": "<p><strong><span style=\"color:#FF0000\">অতি প্রবল ঘূর্ণিঝড় &ldquo;অর্ণব&rdquo; উপকূল অতিক্রম করতে পারে।"
                "</span></strong></p>\r\n<p>&nbsp;</p>"}]}}


@pytest.mark.django_db
def test_json_scroll_feed_new_message_per_text(feed, http):
    """BMD's homepage news scroll comes from a JSON endpoint; each new scroll text is a new message."""
    import json

    url = "https://server8.bmd.gov.bd/bn/slider_json/"
    _configure(feed, "web_page", [url], settings={"dedupe": {"key_from": "content_hash"}})
    ExtractionRule.objects.create(feed=feed, name="scroll", position=10, rule_type="json_fields", on_no_match="fail",
                                  config={"items_path": "data.Scroll", "skip_items_without": ["body_html"],
                                          "fields": {"body_html": "setvalue", "date": "timeUpdate"}})
    ExtractionRule.objects.create(feed=feed, name="date", position=20, rule_type="date",
                                  config={"source": "field", "field": "date"})
    http.get(url, body=json.dumps(BMD_SCROLL, ensure_ascii=False).encode("utf-8"),
             headers={"Content-Type": "text/html; charset=UTF-8"})  # served with the wrong type on purpose
    ctx = run_feed(feed)
    assert ctx.status == "success", ctx.events
    message = CollectedMessage.objects.get()
    assert message.raw_artifact.file_type == "json"
    assert message.body_text == "অতি প্রবল ঘূর্ণিঝড় “অর্ণব” উপকূল অতিক্রম করতে পারে।"
    assert message.title == message.body_text
    assert message.published_at == dt.datetime(2026, 10, 27, 4, 41, 59, tzinfo=dt.timezone.utc)

    run_feed(feed)
    assert CollectedMessage.objects.count() == 1
    BMD_SCROLL["data"]["Scroll"][0]["setvalue"] = "<p>ঘূর্ণিঝড় দুর্বল হয়ে নিম্নচাপে পরিণত হয়েছে।</p>"
    http.replace(responses.GET, url, body=json.dumps(BMD_SCROLL, ensure_ascii=False).encode("utf-8"),
                 headers={"Content-Type": "application/json"})
    run_feed(feed)
    assert sorted(CollectedMessage.objects.values_list("status", flat=True)) == ["new", "new"]


@pytest.mark.django_db
def test_web_page_pagination_by_pattern(feed, http):
    _configure(feed, "web_page", ["https://dae.gov.bd/notices"],
               settings={"scope": {"pagination": {"url_pattern": "https://dae.gov.bd/notices?page={page}", "max_pages": 3}}},
               method_config={"follow_pagination": True})
    for i, url in enumerate(["https://dae.gov.bd/notices", "https://dae.gov.bd/notices?page=2", "https://dae.gov.bd/notices?page=3"]):
        http.get(url, body=f"<html><body><p>Agro advisory page number {i + 1}</p></body></html>", headers={"Content-Type": "text/html"})
    run_feed(feed)
    assert CollectedMessage.objects.count() == 3


# --- listing_to_documents ------------------------------------------------------------
LISTING = """<html><body><table class="list">
<tr class="row"><td><a href="/files/flood-27.pdf">বন্যা বুলেটিন ২৭ অক্টোবর</a></td><td class="d">27/10/2026</td></tr>
<tr class="row"><td><a href="/files/old.pdf">Old bulletin</a></td><td class="d">01/01/2020</td></tr>
<tr class="row"><td><a href="/files/advisory.docx">Advisory (Word)</a></td><td class="d">27/10/2026</td></tr>
<tr class="row"><td><a href="https://elsewhere.org/x.pdf">External</a></td><td class="d">27/10/2026</td></tr>
<tr class="row"><td><a href="/private/secret.pdf">Private</a></td><td class="d">27/10/2026</td></tr>
</table><a class="next" href="/notices/page-2">Next</a></body></html>"""
PAGE2 = """<html><body><table class="list"><tr class="row"><td><a href="/files/cyclone.pdf">Cyclone notice</a></td>
<td class="d">26/10/2026</td></tr></table></body></html>"""


@pytest.mark.django_db
def test_listing_to_documents_scope_pagination_robots_lookback_known(feed, http):
    _configure(feed, "listing_to_documents", ["https://ffwc.gov.bd/notices"], settings={
        "fetch": {"respect_robots": True},
        "files": {"allowed_types": ["html", "pdf"], "disallowed_action": "ignore"},
        "scope": {"include_patterns": ["*.pdf", "*.docx"], "same_domain_only": True,
                  "pagination": {"next_selector": "a.next", "max_pages": 2}},
    }, method_config={"item_selector": "tr.row", "item_date_selector": "td.d"})
    feed.lookback_days = 30
    feed.first_run_done = True
    feed.save()
    http.get("https://ffwc.gov.bd/robots.txt", body="User-agent: *\nDisallow: /private/\n")
    http.get("https://ffwc.gov.bd/notices", body=LISTING, headers={"Content-Type": "text/html"})
    http.get("https://ffwc.gov.bd/notices/page-2", body=PAGE2, headers={"Content-Type": "text/html"})
    http.get("https://ffwc.gov.bd/files/flood-27.pdf", body=make_pdf(["Flood bulletin: Jamuna above danger level"]),
             headers={"Content-Type": "application/pdf"})
    http.get("https://ffwc.gov.bd/files/cyclone.pdf", body=make_pdf(["Cyclone notice text"]),
             headers={"Content-Type": "application/pdf"})
    advisory = http.get("https://ffwc.gov.bd/files/advisory.docx", body=make_docx(["Advisory"]))

    import django.utils.timezone as tz
    from unittest import mock

    fixed_now = dt.datetime(2026, 10, 28, 6, 0, tzinfo=dt.timezone.utc)
    with mock.patch.object(tz, "now", return_value=fixed_now):
        ctx = run_feed(feed)
    titles = dict(CollectedMessage.objects.values_list("source_url", "title"))
    assert titles == {"https://ffwc.gov.bd/files/flood-27.pdf": "বন্যা বুলেটিন ২৭ অক্টোবর",
                      "https://ffwc.gov.bd/files/cyclone.pdf": "Cyclone notice"}
    assert ctx.stats["skipped_old"] == 1 and ctx.stats["out_of_scope"] >= 1 and ctx.stats["disallowed"] == 1
    assert advisory.call_count == 0  # docx not allowed -> never downloaded
    assert any("robots.txt disallows" in e["message"] for e in ctx.events)
    flood = CollectedMessage.objects.get(source_url="https://ffwc.gov.bd/files/flood-27.pdf")
    assert flood.published_at_text == "27/10/2026" and "Jamuna" in flood.body_text

    calls_before = len(http.calls)
    with mock.patch.object(tz, "now", return_value=fixed_now):
        second = run_feed(feed)
    assert second.stats["skipped_known"] == 2
    assert all(not c.request.url.endswith(".pdf") for c in http.calls[calls_before:])  # nothing re-downloaded


# --- RSS -----------------------------------------------------------------------------
RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Alerts</title>
<item><title>ঘূর্ণিঝড় সতর্কবার্তা</title><link>https://ddm.gov.bd/alert/1</link><guid>alert-1</guid>
<pubDate>Tue, 27 Oct 2026 03:00:00 GMT</pubDate><description><![CDATA[<p>চট্টগ্রাম বন্দরে ৩ নম্বর সংকেত।</p>]]></description></item>
<item><title>Heatwave advisory</title><link>https://ddm.gov.bd/alert/2</link>
<pubDate>Mon, 26 Oct 2026 08:00:00 GMT</pubDate><description>Avoid outdoor work at noon.</description></item>
</channel></rss>"""


@pytest.mark.django_db
def test_rss_feed(feed, http):
    _configure(feed, "rss_atom", ["https://ddm.gov.bd/rss.xml"])
    http.get("https://ddm.gov.bd/rss.xml", body=RSS.encode("utf-8"), headers={"Content-Type": "application/rss+xml"})
    run_feed(feed)
    first = CollectedMessage.objects.get(source_item_key="alert-1")
    assert first.title == "ঘূর্ণিঝড় সতর্কবার্তা" and "৩ নম্বর সংকেত" in first.body_text
    assert first.published_at == dt.datetime(2026, 10, 27, 3, 0, tzinfo=dt.timezone.utc)
    assert CollectedMessage.objects.get(source_url="https://ddm.gov.bd/alert/2").source_item_key == "https://ddm.gov.bd/alert/2"
    assert run_feed(feed).stats["unchanged"] == 2


@pytest.mark.django_db
def test_fetch_errors_are_logged_never_silent(feed, http):
    _configure(feed, "web_page", ["https://bmd.gov.bd/down", "https://bmd.gov.bd/up"])
    http.get("https://bmd.gov.bd/down", status=503)
    http.get("https://bmd.gov.bd/up", body="<html><body>All fine and working today</body></html>", headers={"Content-Type": "text/html"})
    ctx = run_feed(feed)
    assert ctx.run.status == "partial"
    assert ctx.run.events.filter(level="error", message__contains="HTTP 503").exists()
    assert CollectedMessage.objects.count() == 1
