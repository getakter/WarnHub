"""Configuration export / import: round trip, conflict modes, dry run, atomicity, UI."""
import json

import pytest
from django.urls import reverse

from apps.core.models import ConfigVersion
from apps.mailbox.models import MailboxAccount, MailboxRule
from apps.sources.models import CustomFieldDefinition, ExtractionRule, Feed, FormatProfile, Keyword, KeywordSet, Source
from apps.sources.portability import export_all, export_config, import_config

pytestmark = pytest.mark.django_db

VOLATILE = {"exported_at", "exported_by", "note"}


def _strip(data):
    return {k: v for k, v in data.items() if k not in VOLATILE}


def _wipe():
    MailboxRule.objects.all().delete()
    MailboxAccount.objects.all().delete()
    Feed.objects.all().delete()
    Source.objects.all().delete()
    FormatProfile.objects.all().delete()
    KeywordSet.objects.all().delete()


def test_full_round_trip(seeded):
    feed = Feed.objects.get(source__code="FFWC")
    CustomFieldDefinition.objects.create(feed=feed, name="bulletin_number", extractor="label_after",
                                         config={"labels": ["বুলেটিন নং"]})
    from datetime import time

    feed.active_hours_start, feed.active_hours_end = time(6), time(20)
    feed.save()
    before = export_all()
    assert json.loads(json.dumps(before, ensure_ascii=False)) == before  # pure JSON
    _wipe()
    report = import_config(before, dry_run=False)
    assert report.ok and report.applied, report.errors
    after = export_all()
    assert _strip(after) == _strip(before)
    assert Keyword.objects.get(term="বন্যা").match_mode == "prefix"
    assert MailboxRule.objects.filter(target_feed__source__code="FFWC").exists()
    assert ConfigVersion.objects.filter(change_type="import").exists()


def test_single_feed_export_is_self_contained(seeded):
    feed = Feed.objects.get(source__code="DAE")
    data = export_config(feeds=[feed])
    assert [s["code"] for s in data["sources"]] == ["DAE"]
    assert [p["slug"] for p in data["profiles"]] == ["notice-listing-rows"]
    assert len(data["keyword_sets"]) == 1 and data["feeds"][0]["rules"]
    _wipe()
    report = import_config(data, dry_run=False)
    assert report.applied and Feed.objects.get().rules.count() == len(data["feeds"][0]["rules"])


def test_conflict_modes(seeded):
    data = export_config(feeds=[Feed.objects.get(source__code="BMD")])
    data["feeds"][0]["description"] = "changed in the file"
    data["feeds"][0]["rules"] = data["feeds"][0]["rules"][:1]

    skip = import_config(data, on_conflict="skip", dry_run=False)
    assert {a[2] for a in skip.actions} == {"skip"}
    assert "CONFIGURE ME" in Feed.objects.get(source__code="BMD").description

    update = import_config(data, on_conflict="update", dry_run=False)
    assert update.applied
    feed = Feed.objects.get(source__code="BMD")
    assert feed.description == "changed in the file" and feed.rules.count() == 1

    copy = import_config(data, on_conflict="copy", dry_run=False)
    assert copy.applied
    assert Source.objects.filter(code="BMD-copy").exists()
    copied_feed = Feed.objects.get(source__code="BMD-copy")
    assert copied_feed.profile.slug == "single-page-bulletin-copy"  # references follow the copies


def test_dry_run_and_errors_change_nothing(seeded):
    data = export_all()
    _wipe()
    dry = import_config(data, dry_run=True)
    assert dry.ok and not dry.applied and dry.counts()["create"] > 5
    assert not Source.objects.exists()

    data["feeds"][0]["rules"][0]["config"] = {"bogus": True}
    data["feeds"][0]["rules"][0]["rule_type"] = "listing_items"
    bad = import_config(data, dry_run=False)
    assert not bad.applied and any("item_selector" in e for e in bad.errors)
    assert not Source.objects.exists()  # all or nothing

    assert import_config({"hello": 1}).errors[0].startswith("This is not a WarnHub configuration export")
    missing = dict(data, sources=[], feeds=[{"slug": "x", "source": "NOPE", "name": "x"}], mailboxes=[])
    assert any("Source 'NOPE'" in e for e in import_config(missing, dry_run=False).errors)


def test_import_export_ui(seeded, admin_client, operator_client):
    response = operator_client.get(reverse("ui:export_all"))
    data = json.loads(response.content)
    assert data["format"] == "ewce-config" and response["Content-Disposition"].startswith("attachment")
    feed = Feed.objects.first()
    assert json.loads(operator_client.get(reverse("ui:export_object", args=["feed", feed.pk])).content)["feeds"]
    assert operator_client.get(reverse("ui:config_io")).status_code == 403

    page = admin_client.post(reverse("ui:config_io"), {"text": json.dumps(data), "on_conflict": "copy", "dry_run": "on"})
    assert page.status_code == 200 and page.context["report"].ok and not page.context["report"].applied
    assert not Source.objects.filter(code__endswith="-copy").exists()
    admin_client.post(reverse("ui:config_io"), {"text": json.dumps(data), "on_conflict": "copy"})
    assert Source.objects.filter(code__endswith="-copy").count() == 4
    bad = admin_client.post(reverse("ui:config_io"), {"text": "{oops", "on_conflict": "skip"})
    assert "Not valid JSON" in bad.content.decode()


def test_clone_and_rollback_still_work_after_import(seeded):
    data = export_config(feeds=[Feed.objects.get(source__code="FFWC")])
    _wipe()
    import_config(data, dry_run=False)
    rule = ExtractionRule.objects.filter(rule_type="pdf").first()
    from apps.core.versioning import rollback, versions_for

    original = dict(rule.config)
    rule.config = {**original, "layout": "tables_as_text"}
    rule.save()
    rollback(rule, versions_for(rule).last())
    rule.refresh_from_db()
    assert rule.config == original
