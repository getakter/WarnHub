import pytest

from apps.core.merge import deep_merge, merge_layers, prune
from apps.sources.config import effective_settings, provenance_rows
from apps.sources.models import ExtractionRule, Feed, FormatProfile, Source


def test_deep_merge_dicts_merge_lists_replace_none_inherits():
    base = {"fetch": {"timeout_seconds": 30, "headers": {"A": "1"}}, "files": {"allowed_types": ["html", "pdf"]}}
    over = {"fetch": {"headers": {"B": "2"}, "retries": None}, "files": {"allowed_types": ["pdf"]}}
    merged = deep_merge(base, over)
    assert merged["fetch"] == {"timeout_seconds": 30, "headers": {"A": "1", "B": "2"}}
    assert merged["files"]["allowed_types"] == ["pdf"]
    assert base["files"]["allowed_types"] == ["html", "pdf"]  # not mutated


def test_merge_layers_records_provenance():
    merged, prov = merge_layers([
        ("default", {"fetch": {"timeout_seconds": 30, "retries": 3}}),
        ("source", {"fetch": {"timeout_seconds": 60}}),
        ("feed", {"fetch": {"retries": 5}}),
    ])
    assert merged["fetch"] == {"timeout_seconds": 60, "retries": 5}
    assert prov["fetch.timeout_seconds"] == "source"
    assert prov["fetch.retries"] == "feed"


def test_prune_drops_empty_but_keeps_false_and_zero():
    assert prune({"a": "", "b": None, "c": {}, "d": False, "e": 0, "f": {"g": ""}}) == {"d": False, "e": 0}


@pytest.mark.django_db
def test_effective_settings_full_chain():
    source = Source.objects.create(name="S", code="S", default_language="bn", default_timezone="Asia/Dhaka",
                                   default_headers={"Accept-Language": "bn"},
                                   settings={"fetch": {"timeout_seconds": 45}})
    profile = FormatProfile.objects.create(name="P", slug="p", settings={
        "fetch": {"timeout_seconds": 50, "retries": 1}, "files": {"allowed_types": ["pdf"]}})
    feed = Feed.objects.create(source=source, profile=profile, name="F", slug="f",
                               settings={"fetch": {"retries": 4}})
    rule = ExtractionRule.objects.create(feed=feed, name="R", rule_type="regex", config={"pattern": "x"},
                                         settings_override={"files": {"allowed_types": ["pdf", "docx"]}})

    merged, prov = effective_settings(feed, with_provenance=True)
    assert merged["content"]["language"] == "bn" and prov["content.language"] == "source"
    assert merged["fetch"]["headers"] == {"Accept-Language": "bn"}
    assert merged["fetch"]["timeout_seconds"] == 50 and prov["fetch.timeout_seconds"] == "profile"
    assert merged["fetch"]["retries"] == 4 and prov["fetch.retries"] == "feed"
    assert merged["files"]["allowed_types"] == ["pdf"]
    assert merged["fetch"]["verify_ssl"] is True and prov.get("fetch.verify_ssl") == "default"

    with_rule = effective_settings(feed, rule)
    assert with_rule["files"]["allowed_types"] == ["pdf", "docx"]

    lower = effective_settings(feed, upto="feed")
    assert lower["fetch"]["retries"] == 1  # what the feed would inherit

    rows = {path: layer for path, _v, _label, layer in provenance_rows(merged, prov)}
    assert rows["fetch.retries"] == "feed"
