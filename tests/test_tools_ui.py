"""Inspect URL, playground, rule/field tests (unsaved config, history), Test feed and Run now."""
import pytest
import responses
from django.urls import reverse

from apps.acquisition.http import clear_robots_cache
from apps.collection.models import CollectedMessage, FetchRun
from apps.sources.models import ExtractionRule

pytestmark = pytest.mark.django_db

PAGE = """<html><head><title>Notices</title><script>alert('x')</script></head><body onload="evil()">
<ul class="notices"><li class="item"><a href="/n/1.pdf">বন্যা সতর্কবার্তা</a> 27/10/2026</li>
<li class="item"><a href="/n/2.pdf">Cyclone signal 3</a> 26/10/2026</li>
<li class="item"><a href="/n/3.html">Heatwave notice</a> 25/10/2026</li></ul>
<p>Signal No. 3 hoisted</p></body></html>"""


@pytest.fixture
def http():
    clear_robots_cache()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.get("https://ddm.gov.bd/robots.txt", status=404)
        mock.get("https://ddm.gov.bd/notices", body=PAGE.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"})
        yield mock


@pytest.fixture
def web_feed(feed):
    feed.method = "web_page"
    feed.entry_urls = ["https://ddm.gov.bd/notices"]
    feed.settings = {**feed.settings, "fetch": {"per_domain_delay_seconds": 0, "retries": 0}}
    feed.save()
    return feed


def _inspect(operator_client, web_feed):
    response = operator_client.get(reverse("ui:inspect_url"), {"url": "https://ddm.gov.bd/notices", "feed": web_feed.pk})
    assert response.status_code == 200
    ctx = response.context
    assert ctx["file_type"] == "html" and ctx["document"].title == "Notices"
    assert ctx["document_links"] == 2 and ctx["links"][0]["is_document"]
    assert any("li.item" in s["selector"] for s in ctx["suggestions"])
    assert "alert(" not in ctx["preview"] and "onload" not in ctx["preview"]  # neutralized preview
    return ctx["token"]


def test_inspect_url_page(operator_client, http, web_feed):
    assert _inspect(operator_client, web_feed)


def test_playground_css_xpath_regex(operator_client, http, web_feed):
    token = _inspect(operator_client, web_feed)
    url = reverse("ui:playground")
    css = operator_client.post(url, {"token": token, "kind": "css", "expression": "li.item a"}).json()
    assert css["count"] == 3 and css["matches"][0]["text"] == "বন্যা সতর্কবার্তা" and "ewce-hit" in css["preview"]
    xpath = operator_client.post(url, {"token": token, "kind": "xpath", "expression": "//li/a/@href"}).json()
    assert [m["text"] for m in xpath["matches"]] == ["/n/1.pdf", "/n/2.pdf", "/n/3.html"]
    regex = operator_client.post(url, {"token": token, "kind": "regex", "expression": r"Signal No\. (?P<n>\d)"}).json()
    assert regex["count"] == 1 and regex["matches"][0]["groups"] == {"n": "3"} and "<mark>" in regex["highlighted"]
    bad = operator_client.post(url, {"token": token, "kind": "css", "expression": "li["}).json()
    assert "Invalid CSS selector" in bad["error"]
    expired = operator_client.post(url, {"token": "nope", "kind": "css", "expression": "a"})
    assert expired.status_code == 410


def _rule_post(**extra):
    data = {"rule_type": "listing_items", "name": "Rows", "enabled": "on", "on_no_match": "continue",
            "c-item_selector": "li.item", "c-fields": "title = a\nlink = a@href", "c-skip_items_without": "title",
            "c-body_from": "item_text", "_test_target": "rule", "_test_mode": "url",
            "_test_url": "https://ddm.gov.bd/notices"}
    data.update(extra)
    return data


def test_rule_test_on_url_uses_unsaved_config(admin_client, http, web_feed):
    response = admin_client.post(reverse("ui:test_extraction", args=[web_feed.pk]), _rule_post())
    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["highlight"]["count"] == 3
    titles = [r["title"] for r in response.context["results"]]
    assert titles == ["বন্যা সতর্কবার্তা", "Cyclone signal 3", "Heatwave notice"]
    assert "https://ddm.gov.bd/n/1.pdf" in html
    assert not ExtractionRule.objects.exists() and not CollectedMessage.objects.exists()  # nothing saved
    invalid = admin_client.post(reverse("ui:test_extraction", args=[web_feed.pk]), _rule_post(**{"c-item_selector": "li["}))
    assert "not a valid CSS selector" in invalid.content.decode()


def test_rule_test_on_history_and_file(admin_client, http, web_feed):
    from apps.collection.pipeline import run_feed

    run_feed(web_feed)  # stores the page as one message (no rules yet)
    response = admin_client.post(reverse("ui:test_extraction", args=[web_feed.pk]), _rule_post(_test_mode="history"))
    row = response.context["history"][0]
    assert len(row["stored"]) == 1 and [r["status"] for r in row["results"]] == ["new", "new", "new"]
    assert row["changed"] == 3

    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("page.html", PAGE.encode("utf-8"), content_type="text/html")
    response = admin_client.post(reverse("ui:test_extraction", args=[web_feed.pk]), _rule_post(_test_mode="file", _test_file=upload))
    assert len(response.context["results"]) == 3


def test_field_test(admin_client, http, web_feed):
    response = admin_client.post(reverse("ui:test_extraction", args=[web_feed.pk]), {
        "_test_target": "field", "_test_mode": "url", "_test_url": "https://ddm.gov.bd/notices", "extractor": "regex",
        "name": "signal", "enabled": "on", "c-pattern": r"Signal No\. (\d)", "c-group": "1", "c-source_field": "body_text",
    })
    assert response.context["results"][0]["extra_fields"] == {"signal": "3"}


def test_operator_cannot_test_rules_but_can_run(operator_client, http, web_feed):
    assert operator_client.post(reverse("ui:test_extraction", args=[web_feed.pk]), _rule_post()).status_code == 403
    response = operator_client.post(reverse("ui:feed_test", args=[web_feed.pk]))
    assert response.status_code == 200 and response.context["results"][0]["status"] == "new"
    assert not FetchRun.objects.exists()
    response = operator_client.post(reverse("ui:feed_run_now", args=[web_feed.pk]))
    run = FetchRun.objects.get()
    assert response["Location"] == run.get_absolute_url() and run.status == "success"
    assert CollectedMessage.objects.count() == 1


def test_rule_and_field_pages_have_test_panel(admin_client, web_feed):
    page = admin_client.get(reverse("ui:rule_create", args=[web_feed.pk]) + "?type=html_selector").content.decode()
    assert "test-panel" in page and "Test on history" in page
    page = admin_client.get(reverse("ui:field_create", args=[web_feed.pk])).content.decode()
    assert "test-panel" in page
