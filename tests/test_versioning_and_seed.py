import pytest

from apps.core.versioning import rollback, versions_for
from apps.mailbox.models import MailboxAccount, MailboxRule
from apps.sources import services
from apps.sources.models import (
    CustomFieldDefinition, ExtractionRule, Feed, FormatProfile, Keyword, KeywordSet, Source,
)
from apps.ui.views.config import parse_keyword_csv

pytestmark = pytest.mark.django_db


def test_versions_recorded_deduplicated_and_rollback():
    source = Source.objects.create(name="S", code="S")
    feed = Feed.objects.create(source=source, name="F", slug="f", settings={"fetch": {"retries": 1}})
    assert versions_for(feed).count() == 1
    feed.save()  # no change -> no new version
    assert versions_for(feed).count() == 1
    feed.settings = {"fetch": {"retries": 9}}
    feed.save()
    feed.health_status = "failing"
    feed.save(update_fields=["health_status"])  # runtime state is not versioned
    versions = list(versions_for(feed))
    assert [v.version for v in versions] == [2, 1]
    rollback(feed, versions[1])
    feed.refresh_from_db()
    assert feed.settings == {"fetch": {"retries": 1}}
    assert feed.health_status == "failing"  # rollback leaves runtime state alone
    assert versions_for(feed).first().change_type == "rollback"


def test_keyword_set_snapshot_includes_keywords_and_rolls_back():
    from apps.core.versioning import record_version

    kset = KeywordSet.objects.create(name="K")
    Keyword.objects.create(keyword_set=kset, term="flood")
    record_version(kset)
    good = versions_for(kset).first()
    Keyword.objects.create(keyword_set=kset, term="bad term")
    record_version(kset)
    rollback(kset, good)
    assert list(kset.keywords.values_list("term", flat=True)) == ["flood"]


def test_seed_is_valid_idempotent_and_safe(seeded):
    from apps.sources.seed import seed_all

    assert seed_all() == {"profiles": 0, "feeds": 0, "mailbox": False}
    assert Source.objects.filter(is_example=True).count() == 4
    for feed in Feed.objects.all():
        assert not feed.is_active and feed.entry_urls == [] and "CONFIGURE ME" in feed.description
        feed.full_clean()
    for model in (Source, FormatProfile, ExtractionRule, CustomFieldDefinition, MailboxAccount, MailboxRule):
        for obj in model.objects.all():
            obj.full_clean()
    assert MailboxAccount.objects.get().is_active is False
    kset = KeywordSet.objects.get()
    assert kset.keywords.filter(language="bn", match_mode="prefix").count() == 9
    assert kset.keywords.get(term="বন্যা").tag == "flood"


def test_seed_examples_are_deletable(seeded):
    feed = Feed.objects.get(source__code="DAE")
    feed.delete()
    Source.objects.get(code="DAE").delete()
    assert not Source.objects.filter(code="DAE").exists()


def test_clone_feed_copies_rules_fields_and_keywords(seeded):
    feed = Feed.objects.get(source__code="FFWC")
    CustomFieldDefinition.objects.create(feed=feed, name="bulletin_number", extractor="regex", config={"pattern": "No (\\d+)"})
    copy = services.clone_feed(feed)
    assert copy.pk != feed.pk and copy.slug != feed.slug and not copy.is_active
    assert copy.rules.count() == feed.rules.count() > 0
    assert copy.custom_fields.get().name == "bulletin_number"
    assert list(copy.keyword_sets.all()) == list(feed.keyword_sets.all())
    assert versions_for(copy).first().change_type == "clone"


def test_profile_clone_and_feed_from_profile(seeded):
    profile = FormatProfile.objects.get(slug="sectioned-bulletin")
    copy = services.clone_profile(profile)
    assert copy.slug == "sectioned-bulletin-2" or copy.slug.startswith("bulletin-split") or copy.slug != profile.slug
    feed = services.create_feed_from_profile(Source.objects.get(code="BMD"), copy, "Division bulletin")
    assert [r.rule_type for r in feed.rules.all()] == ["html_selector", "section_split", "date"]
    assert feed.method == "web_page"
    again = services.save_feed_as_profile(feed, "From feed")
    assert len(again.rules_template) == 3


def test_reorder_rules(seeded):
    feed = Feed.objects.get(source__code="FFWC")
    ids = list(feed.rules.values_list("pk", flat=True))
    services.reorder(feed.rules.all(), list(reversed(ids)))
    assert list(feed.rules.values_list("pk", flat=True)) == list(reversed(ids))


def test_keyword_csv_parse_bangla_and_errors():
    rows, errors = parse_keyword_csv(
        "﻿term,language,variants,kind,match_mode,tag,weight\n"
        "ঘূর্ণিঝড়,bn,ঘুর্ণিঝড়|সাইক্লোন,include,prefix,cyclone,2\n"
        "flood,en,,exclude,whole_word,,\n"
        "x,zz,,,,,\n"
    )
    assert rows[0]["term"] == "ঘূর্ণিঝড়" and rows[0]["variants"] == ["ঘুর্ণিঝড়", "সাইক্লোন"] and rows[0]["weight"] == 2.0
    assert rows[1]["kind"] == "exclude"
    assert errors and "language" in errors[0]
