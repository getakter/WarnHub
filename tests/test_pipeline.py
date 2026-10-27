"""Pipeline core: dedupe/versioning, filtering, failures, legacy fonts, locks, dry runs, children."""
import pytest

from apps.acquisition.base import Acquired
from apps.collection import locks
from apps.collection.models import CollectedMessage, FetchRun, RawArtifact
from apps.collection.pipeline import RunContext, ingest_upload, process_document, run_feed
from apps.sources.models import ExtractionRule, Feed, Keyword, KeywordSet
from tests.helpers import make_eml, make_pdf, make_zip

pytestmark = pytest.mark.django_db

BN_TEXT = "আগামী ২৪ ঘণ্টায় সিলেট বিভাগে ভারী বৃষ্টি ও বন্যার সম্ভাবনা রয়েছে।"


def _doc(url, text, **kw):
    return {"data": text.encode("utf-8"), "url": url, "content_type": "text/plain; charset=utf-8", **kw}


def _keywords(feed, *rows):
    kset = KeywordSet.objects.create(name=f"K{feed.pk}")
    for term, kind, mode, lang in rows:
        Keyword.objects.create(keyword_set=kset, term=term, kind=kind, match_mode=mode, language=lang)
    feed.keyword_sets.add(kset)
    return kset


def test_upload_creates_message_with_artifact_and_normalized_search(feed):
    _keywords(feed, ("বন্যা", "include", "prefix", "bn"), ("flood", "include", "whole_word", "en"))
    ctx = ingest_upload(feed, text=BN_TEXT, title="সতর্কবার্তা")
    assert ctx.status == "success"
    message = CollectedMessage.objects.get()
    assert message.status == "new" and message.title == "সতর্কবার্তা" and message.manual_entry
    assert message.detected_language == "bn"
    assert "24" in message.search_text  # Bangla digits searchable as ASCII
    assert [k["term"] for k in message.matched_keywords] == ["বন্যা"]
    assert message.matched_keywords[0]["matched"] == "বন্যা"  # prefix matched inside বন্যার
    assert message.raw_artifact.origin == "paste" and message.fetch_run.trigger == "upload"


def test_rerun_is_idempotent_and_changes_create_linked_versions(feed, static_docs):
    static_docs.append(_doc("https://met.example/today", "Forecast: light rain in Dhaka today."))
    first = run_feed(feed)
    second = run_feed(feed)
    assert first.stats["new"] == 1 and second.stats["unchanged"] == 1
    assert CollectedMessage.objects.count() == 1
    assert CollectedMessage.objects.get().seen_count == 2
    static_docs[0] = _doc("https://met.example/today", "Forecast: heavy rain and flood warning in Dhaka today.")
    run_feed(feed)
    v1, v2 = CollectedMessage.objects.order_by("version")
    assert (v1.version, v2.version) == (1, 2)
    assert v2.status == "updated" and v2.supersedes == v1 and not v1.is_latest and v2.is_latest
    assert list(v2.version_chain()) == [v2, v1]
    assert RawArtifact.objects.count() == 2


def test_versioning_off_logs_change_only(feed, static_docs):
    feed.settings = {**feed.settings, "dedupe": {"version_on_change": False}}
    feed.save()
    static_docs.append(_doc("https://met.example/a", "Version one of the text"))
    run_feed(feed)
    static_docs[0] = _doc("https://met.example/a", "Version two of the text")
    ctx = run_feed(feed)
    assert ctx.stats["changed_ignored"] == 1 and CollectedMessage.objects.count() == 1


def test_same_content_new_key_is_duplicate(feed, static_docs):
    static_docs.extend([_doc("https://met.example/a.txt", "Identical bulletin text here"),
                        _doc("https://mirror.example/a.txt", "Identical bulletin text here")])
    run_feed(feed)
    original, dup = CollectedMessage.objects.order_by("id")
    assert dup.status == "duplicate" and dup.duplicate_of == original


def test_keyword_capture_modes_never_discard(feed, static_docs):
    _keywords(feed, ("cyclone", "include", "whole_word", "en"), ("test message", "exclude", "substring", "en"))
    static_docs.extend([
        _doc("https://x/1", "Cyclone signal 3 hoisted for Chattogram"),
        _doc("https://x/2", "Weekly office notice about holidays"),
        _doc("https://x/3", "This is a test message about a cyclone"),
    ])
    feed.settings = {**feed.settings, "capture": {"mode": "keyword_match_only", "min_text_length": 5}}
    feed.save()
    run_feed(feed)
    statuses = dict(CollectedMessage.objects.values_list("source_url", "status"))
    assert statuses == {"https://x/1": "new", "https://x/2": "filtered_out", "https://x/3": "filtered_out"}
    assert "exclude keyword" in CollectedMessage.objects.get(source_url="https://x/3").attention_reason

    CollectedMessage.objects.all().delete()
    feed.settings = {**feed.settings, "capture": {"mode": "keyword_match_or_tag", "min_text_length": 5}}
    feed.save()
    run_feed(feed)
    office = CollectedMessage.objects.get(source_url="https://x/2")
    assert office.status == "new" and "no_keyword_match" in office.quality_flags


def test_ignore_title_patterns(feed, static_docs):
    feed.settings = {**feed.settings, "capture": {"ignore_title_patterns": ["^Tender"], "min_text_length": 5}}
    feed.save()
    static_docs.append(_doc("https://x/t", "Tender notice for office supplies"))
    run_feed(feed)
    assert CollectedMessage.objects.get().status == "filtered_out"


def test_legacy_font_text_is_flagged_not_stored_as_clean_text(feed):
    garbled = "evsjv‡`k AvenvIqv Awa`ß‡ii c~e©vfvm: AvMvgx 24 N›Uvi g‡a¨ †`‡ki wewfbœ ¯’v‡b e„wócvZ n‡Z cv‡i|"
    ingest_upload(feed, text=garbled)
    message = CollectedMessage.objects.get()
    assert message.status == "needs_attention"
    assert "legacy_bangla_font" in message.quality_flags
    assert message.body_text == "" and "AvenvIqv" in message.raw_extracted_text
    assert "Legacy" in message.attention_reason


def test_corrupt_file_becomes_extraction_failed_once(feed, static_docs):
    static_docs.append({"data": b"%PDF-1.4 this is not really a pdf", "url": "https://x/broken.pdf"})
    ctx = run_feed(feed)
    assert ctx.status == "partial" or ctx.status == "success"
    message = CollectedMessage.objects.get()
    assert message.status == "extraction_failed" and message.raw_artifact is not None
    assert "Parse error" in message.attention_reason
    run_feed(feed)
    assert CollectedMessage.objects.count() == 1  # repeated failure is a sighting, not a new row


def test_rule_on_no_match_flag_and_fail(feed, static_docs):
    rule = ExtractionRule.objects.create(feed=feed, name="never", rule_type="test_no_match", on_no_match="flag")
    static_docs.append(_doc("https://x/r", "Some bulletin text for rules"))
    run_feed(feed)
    message = CollectedMessage.objects.get()
    assert message.status == "needs_attention" and "found nothing" in message.attention_reason
    CollectedMessage.objects.all().delete()
    rule.on_no_match = "fail"
    rule.save()
    run_feed(feed)
    failed = CollectedMessage.objects.get()
    assert failed.status == "extraction_failed" and failed.extracting_rule == rule


def test_rule_exception_is_recorded(feed, static_docs):
    ExtractionRule.objects.create(feed=feed, name="explode", rule_type="test_explode")
    static_docs.append(_doc("https://x/e", "Some bulletin text"))
    ctx = run_feed(feed)
    assert CollectedMessage.objects.get().status == "extraction_failed"
    assert any("boom" in e["message"] for e in ctx.events)


def test_producing_rule_splits_and_records_rule(feed, static_docs):
    rule = ExtractionRule.objects.create(feed=feed, name="lines", rule_type="test_split_lines")
    static_docs.append(_doc("https://x/multi", "Dhaka rain likely today\nSylhet flood warning issued"))
    run_feed(feed)
    messages = CollectedMessage.objects.order_by("id")
    assert [m.source_item_key for m in messages] == ["https://x/multi#Dhaka", "https://x/multi#Sylhet"]
    assert all(m.extracting_rule == rule for m in messages)


def test_dry_run_saves_nothing(feed, static_docs):
    static_docs.append(_doc("https://x/d", "Dry run bulletin text"))
    ctx = run_feed(feed, dry_run=True)
    assert ctx.results[0]["status"] == "new"
    assert not CollectedMessage.objects.exists() and not RawArtifact.objects.exists() and not FetchRun.objects.exists()


def test_lock_prevents_overlapping_runs(feed, static_docs):
    assert locks.acquire(Feed, feed.pk, "someone-else")
    ctx = run_feed(feed)
    assert ctx.run.status == "skipped"
    locks.release(Feed, feed.pk, "someone-else")
    assert run_feed(feed).run.status == "success"


def test_feed_state_updated_after_run(feed, static_docs):
    static_docs.append(_doc("https://x/s", "State bulletin text"))
    run_feed(feed)
    feed.refresh_from_db()
    assert feed.last_success_at and feed.last_new_message_at and feed.first_run_done and not feed.lock_owner
    feed.method = "no_such_method"
    feed.save()
    ctx = run_feed(feed)
    feed.refresh_from_db()
    assert ctx.run.status == "failed" and feed.consecutive_failures == 1 and "no_such_method" in feed.last_error


def test_disallowed_types_ignore_store_raw_flag(feed, static_docs):
    pdf = make_pdf(["A pdf bulletin"])
    static_docs.append({"data": pdf, "url": "https://x/a.pdf"})
    for action, messages, artifacts in [("ignore", 0, 0), ("store_raw", 0, 1), ("flag", 1, 1)]:
        CollectedMessage.objects.all().delete()
        RawArtifact.objects.all().delete()
        feed.settings = {"files": {"allowed_types": ["html"], "disallowed_action": action}}
        feed.save()
        ctx = run_feed(feed)
        assert CollectedMessage.objects.count() == messages, action
        assert RawArtifact.objects.count() == artifacts, action
        assert any("not allowed" in e["message"] for e in ctx.events) or messages
    assert CollectedMessage.objects.get().status == "needs_attention"


def test_max_items_per_run_and_lookback(feed, static_docs):
    from datetime import timedelta

    from django.utils import timezone

    feed.max_items_per_run = 2
    feed.save()
    static_docs.extend(_doc(f"https://x/{i}", f"Bulletin number {i} text") for i in range(5))
    ctx = run_feed(feed)
    assert CollectedMessage.objects.count() == 2 and ctx.stats["items"] == 2

    CollectedMessage.objects.all().delete()
    static_docs.clear()
    feed.max_items_per_run = 50
    feed.lookback_days = 3
    feed.first_run_done = True
    feed.save()
    static_docs.append(Acquired(data=b"Old bulletin text", url="https://x/old",
                                hints={"published_at": timezone.now() - timedelta(days=10)}))
    ctx = run_feed(feed)
    assert ctx.stats["skipped_old"] == 1 and not CollectedMessage.objects.exists()


def test_zip_and_email_children_link_to_parent(feed):
    data = make_zip({"one.txt": "First bulletin in zip", "two.txt": "Second bulletin in zip"})
    ingest_upload(feed, files=[("pack.zip", data, "application/zip")])
    messages = CollectedMessage.objects.order_by("id")
    assert [m.raw_artifact.original_filename for m in messages] == ["one.txt", "two.txt"]
    assert all(m.raw_artifact.parent.file_type == "zip" for m in messages)

    CollectedMessage.objects.all().delete()
    eml = make_eml("বন্যা পূর্বাভাস", "Body of the flood email bulletin",
                   attachments=[("b.pdf", make_pdf(["Attached flood bulletin"]), "application", "pdf")])
    ingest_upload(feed, files=[("mail.eml", eml, "message/rfc822")])
    body, attachment = CollectedMessage.objects.order_by("id")
    assert attachment.raw_artifact.origin == "attachment" and attachment.raw_artifact.parent.file_type == "eml"
    assert {body.title, attachment.raw_artifact.original_filename} == {"বন্যা পূর্বাভাস", "b.pdf"}


def test_packet_budget_truncates_proactively(feed, monkeypatch):
    monkeypatch.setattr("apps.collection.pipeline._packet_limit", lambda: 200_000)
    ingest_upload(feed, text="বন্যা " * 40_000)
    message = CollectedMessage.objects.get()
    assert "truncated" in message.quality_flags
    assert len(message.body_text.encode("utf-8")) < 200_000


def test_unknown_binary_upload_is_visible(feed):
    ingest_upload(feed, files=[("blob.bin", b"\x00\x01\x02\x03" * 100, "application/octet-stream")])
    message = CollectedMessage.objects.get()
    assert message.status == "extraction_failed" and message.raw_artifact.file_type == "unknown"


def test_process_document_direct_context(feed):
    ctx = RunContext(feed, trigger="manual")
    results = process_document(ctx, Acquired(data=b"Direct bulletin content", url="https://x/direct"))
    ctx.finish()
    assert results[0]["status"] == "new" and ctx.run.status == "success"
    assert ctx.run.events.filter(level="warning").count() == 0
