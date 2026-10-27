"""Every M1 page renders; roles are enforced; schema forms save through the UI."""
import pytest
from django.urls import reverse

from apps.core.models import ConfigVersion
from apps.sources.models import ExtractionRule, Feed, FormatProfile, KeywordSet, Source

pytestmark = pytest.mark.django_db


def _pages():
    feed = Feed.objects.get(source__code="FFWC")
    rule = feed.rules.first()
    profile = FormatProfile.objects.first()
    kset = KeywordSet.objects.first()
    source = feed.source
    return [
        reverse("ui:dashboard"), reverse("ui:plugins"), reverse("ui:audit"),
        reverse("ui:source_list"), reverse("ui:source_detail", args=[source.pk]),
        reverse("ui:source_create"), reverse("ui:source_edit", args=[source.pk]),
        reverse("ui:feed_list"), reverse("ui:feed_detail", args=[feed.pk]), reverse("ui:feed_create"),
        reverse("ui:feed_edit", args=[feed.pk]), reverse("ui:feed_edit", args=[feed.pk]) + "?method=rss_atom",
        reverse("ui:feed_keyword_sets", args=[feed.pk]),
        reverse("ui:rule_create", args=[feed.pk]), reverse("ui:rule_create", args=[feed.pk]) + "?type=listing_items",
        reverse("ui:rule_edit", args=[feed.pk, rule.pk]),
        reverse("ui:field_create", args=[feed.pk]) + "?extractor=label_after",
        reverse("ui:profile_list"), reverse("ui:profile_detail", args=[profile.pk]),
        reverse("ui:profile_edit", args=[profile.pk]), reverse("ui:profile_create"),
        reverse("ui:keywordset_list"), reverse("ui:keywordset_detail", args=[kset.pk]),
        reverse("ui:keywordset_export_csv", args=[kset.pk]),
        reverse("ui:versions", args=["sources.feed", feed.pk]),
        reverse("ui:versions", args=["sources.extractionrule", rule.pk]),
    ]


def test_all_pages_render_for_admin(seeded, admin_client):
    for url in _pages():
        response = admin_client.get(url)
        assert response.status_code == 200, url


def test_operator_can_view_but_not_configure(seeded, operator_client):
    feed = Feed.objects.get(source__code="FFWC")
    assert operator_client.get(reverse("ui:feed_detail", args=[feed.pk])).status_code == 200
    assert operator_client.get(reverse("ui:feed_edit", args=[feed.pk])).status_code == 403
    assert operator_client.post(reverse("ui:feed_clone", args=[feed.pk])).status_code == 403


def test_anonymous_redirected_to_login(client, seeded):
    response = client.get(reverse("ui:dashboard"))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_feed_edit_saves_layered_settings(seeded, admin_client):
    feed = Feed.objects.get(source__code="FFWC")
    url = reverse("ui:feed_edit", args=[feed.pk])
    form = admin_client.get(url).context["form"]
    data = {name: form[name].value() or "" for name in form.fields}
    data.update({
        "method": "listing_to_documents", "entry_urls": "https://www.ffwc.gov.bd/bulletins?y={YYYY}",
        "tags": "flood\nexample", "is_active": "", "schedule_type": "interval", "interval_minutes": "90",
        "max_items_per_run": "20", "lookback_days": "3", "profile": str(feed.profile_id),
        "s-fetch__timeout_seconds": "45", "s-fetch__verify_ssl": "false",
        "s-files__allowed_types": ["pdf", "docx"], "s-scope__exclude_patterns": "*/archive/*",
        "m-link_text_as_title": "on",
    })
    response = admin_client.post(url, data)
    assert response.status_code == 302, response.context and response.context.get("form").errors
    feed.refresh_from_db()
    assert feed.entry_urls == ["https://www.ffwc.gov.bd/bulletins?y={YYYY}"]
    assert feed.settings["fetch"] == {"timeout_seconds": 45, "verify_ssl": False}
    assert feed.settings["files"]["allowed_types"] == ["pdf", "docx"]
    assert feed.settings["scope"] == {"exclude_patterns": ["*/archive/*"]}
    assert feed.method_config["link_text_as_title"] is True
    assert ConfigVersion.objects.filter(object_id=feed.pk, content_type__model="feed").count() >= 2


def test_rule_create_with_invalid_selector_shows_error(seeded, admin_client):
    feed = Feed.objects.get(source__code="DAE")
    url = reverse("ui:rule_create", args=[feed.pk]) + "?type=listing_items"
    response = admin_client.post(url, {
        "rule_type": "listing_items", "name": "Rows", "enabled": "on", "on_no_match": "continue",
        "c-item_selector": "table tr[", "c-fields": "title = a",
    })
    assert response.status_code == 200
    assert "not a valid CSS selector" in response.content.decode()
    response = admin_client.post(url, {
        "rule_type": "listing_items", "name": "Rows", "enabled": "on", "on_no_match": "continue",
        "c-item_selector": "ul.notices li", "c-fields": "title = a\nlink = a@href",
        "c-skip_items_without": "title", "c-body_from": "item_text",
    })
    assert response.status_code == 302
    rule = ExtractionRule.objects.filter(feed=feed).order_by("-pk").first()
    assert rule.config["item_selector"] == "ul.notices li" and rule.config["fields"]["link"] == "a@href"


def test_validate_pattern_endpoint(seeded, admin_client):
    url = reverse("ui:validate_pattern")
    assert admin_client.post(url, {"format": "regex", "value": "(?P<x>\\d+)"}).json()["ok"] is True
    assert admin_client.post(url, {"format": "css-selector", "value": "a[href"}).json()["ok"] is False
    assert admin_client.post(url, {"format": "xpath", "value": "//div[@id='x']"}).json()["ok"] is True


def test_toggle_feed_refuses_enable_without_urls(seeded, admin_client):
    feed = Feed.objects.get(source__code="BMD")
    admin_client.post(reverse("ui:toggle_active", args=["feed", feed.pk]))
    feed.refresh_from_db()
    assert not feed.is_active


def test_delete_source_with_feeds_is_blocked(seeded, admin_client):
    source = Source.objects.get(code="BMD")
    response = admin_client.post(reverse("ui:source_delete", args=[source.pk]), follow=True)
    assert Source.objects.filter(pk=source.pk).exists()
    assert "Cannot delete" in response.content.decode()


def test_rollback_through_ui(seeded, admin_client):
    rule = ExtractionRule.objects.filter(rule_type="pdf").first()
    original = dict(rule.config)
    rule.config = {**original, "text_mode": "ocr"}
    rule.save()
    first = ConfigVersion.objects.filter(content_type__model="extractionrule", object_id=rule.pk).order_by("version").first()
    admin_client.post(reverse("ui:version_rollback", args=[first.pk]))
    rule.refresh_from_db()
    assert rule.config == original
