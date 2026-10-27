"""Collection pipeline.

Acquire -> Archive raw -> Parse -> Rules (in order) -> Custom fields -> Normalize ->
Keyword filter -> Dedupe / version -> Save -> Log (FetchRun + events)

Nothing is dropped silently: every skip is logged on the run, every parse or rule failure with an
artifact becomes an ``extraction_failed`` message, and filtered items are saved as ``filtered_out``.
"""
import copy
import logging
import re
import traceback
from collections import Counter
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.acquisition.base import Acquired
from apps.core.bangla import search_normalize
from apps.extraction.base import Item, ParsedDocument
from apps.extraction.engine import (
    RuleError, apply_rules, default_items, extract_custom_fields, parse_options, settings_resolver,
)
from apps.extraction.filetypes import detect_file_type
from apps.extraction.keywords import KeywordMatcher, evaluate
from apps.extraction.parsers import parser_for
from apps.plugins.registry import PluginNotFound, registry
from apps.sources.config import builtin_defaults, effective_method_config, effective_settings
from apps.sources.models import Feed, KeywordSet

from . import locks
from .archive import sha256_hex, store_artifact
from .dedupe import classify, content_hash, item_key
from .models import CollectedMessage, FetchRun, FetchRunEvent
from .normalize import finalize_item

logger = logging.getLogger("ewce.pipeline")
MAX_EVENTS_PER_RUN = 1000
PACKET_SAFETY = 0.75


def _packet_limit():
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT @@max_allowed_packet")
            return int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 - non-MySQL backends
        return 64 * 1024 * 1024


class RunContext:
    """State of one run (scheduled, manual, test, upload or mailbox)."""

    def __init__(self, feed=None, *, trigger="manual", user=None, dry_run=False, mailbox=None, worker="",
                 settings=None, honour_limits=True, run=None):
        self.feed = feed
        self.mailbox = mailbox
        self.trigger = trigger
        self.user = user if getattr(user, "is_authenticated", False) else None
        self.dry_run = dry_run
        self.honour_limits = honour_limits
        self.settings = settings or (effective_settings(feed) if feed else builtin_defaults())
        self.rules = list(feed.rules.all()) if feed else []
        self.custom_fields = list(feed.custom_fields.all()) if feed else []
        keyword_sets = (
            KeywordSet.objects.filter(is_active=True, feedkeywordset__feed=feed, feedkeywordset__enabled=True)
            .prefetch_related("keywords") if feed else []
        )
        self.matcher = KeywordMatcher(keyword_sets)
        self.stats = Counter()
        self.events = []
        self.results = []
        self.error_count = 0
        self.fatal = False
        self.packet_budget = int(_packet_limit() * PACKET_SAFETY)
        self.cutoff = None
        if feed and honour_limits and feed.lookback_days and not (feed.backfill_on_first_run and not feed.first_run_done):
            self.cutoff = timezone.now() - timedelta(days=feed.lookback_days)
        self.run = run  # a shared run (mailbox polls) is finished by its owner, not here
        self.owns_run = run is None
        if run is None and not dry_run:
            self.run = FetchRun.objects.create(feed=feed, mailbox=mailbox, trigger=trigger, triggered_by=self.user,
                                               worker=worker[:100])

    # -- logging ----------------------------------------------------------------------
    def log(self, level, message, **context):
        getattr(logger, "warning" if level == "warning" else level if level in {"info", "error", "debug"} else "info")(
            "run=%s feed=%s %s", getattr(self.run, "pk", "dry"), getattr(self.feed, "pk", None), message
        )
        if level == "error":
            self.error_count += 1
        entry = {"level": level, "message": message, "context": context, "at": timezone.now().isoformat()}
        if len(self.events) < MAX_EVENTS_PER_RUN:
            self.events.append(entry)
            if self.run is not None:
                FetchRunEvent.objects.create(run=self.run, level=level, message=message[:5000],
                                             context=_jsonable(context))

    @property
    def limit_reached(self):
        if not (self.honour_limits and self.feed):
            return False
        return self.stats["items"] >= self.feed.max_items_per_run

    def finish(self, status=None, error=""):
        if status is None:
            if self.fatal:
                status = "failed"
            elif self.error_count:
                status = "partial" if self.stats["documents"] else "failed"
            else:
                status = "success"
        self.status = status
        if self.run is not None and self.owns_run:
            self.run.status = status
            self.run.finished_at = timezone.now()
            self.run.stats = dict(self.stats)
            self.run.error_message = error[:5000] or "\n".join(
                e["message"] for e in self.events if e["level"] == "error"
            )[:5000]
            self.run.save(update_fields=["status", "finished_at", "stats", "error_message"])
        return status


def _jsonable(value):
    import json

    from django.core.serializers.json import DjangoJSONEncoder

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, default=str))


def _result(item_title, status, **extra):
    return {"title": item_title, "status": status, **extra}


# --- Saving --------------------------------------------------------------------------
def _fit_packet(ctx, values, flags):
    """Shrink text columns proactively if the row would exceed MariaDB's max_allowed_packet."""
    text_fields = ["body_text", "search_text", "body_html", "raw_extracted_text"]
    size = sum(len(values[f].encode("utf-8")) for f in text_fields) + 20000
    if size <= ctx.packet_budget:
        return
    values["body_html"] = ""
    per_field = max(1000, (ctx.packet_budget - 20000) // 3 // 4)  # utf8mb4: up to 4 bytes per char
    for name in ("body_text", "search_text", "raw_extracted_text"):
        values[name] = values[name][:per_field]
    if "truncated" not in flags:
        flags.append("truncated")
    ctx.log("warning", "Message text truncated to fit the database packet limit (raise max_allowed_packet).",
            title=values["title"][:100])


def _sighting(message):
    CollectedMessage.objects.filter(pk=message.pk).update(seen_count=F("seen_count") + 1, last_seen_at=timezone.now())


def save_item(ctx, item, document, artifact, acq, file_type):
    settings = ctx.settings
    finalize_item(item, document, settings, filename=acq.filename, link_text=acq.link_text)
    ctx.stats["items"] += 1
    ctx.stats["body_chars"] += len(item.body_text or item.raw_text)  # for the layout-change health check

    if ctx.cutoff and item.published_at and item.published_at < ctx.cutoff:
        ctx.stats["skipped_old"] += 1
        ctx.log("info", f"Skipped (older than lookback window): {item.title[:120]}", published=item.published_at)
        return _result(item.title, "skipped_old")

    capture = settings["capture"]
    search_in = capture.get("search_in", "title_and_body")
    text = {"title_only": item.title, "body_only": item.body_text}.get(search_in, f"{item.title}\n{item.body_text}")
    keep, flag, reason, matched, score = evaluate(ctx.matcher.match(text), capture.get("mode", "capture_all"))
    for pattern in capture.get("ignore_title_patterns") or []:
        if re.search(pattern, item.title, re.IGNORECASE):
            keep, reason = False, f"title matches ignore pattern {pattern!r}"
            break
    if flag:
        item.flag(flag)

    digest = content_hash(item, settings)
    key = item_key(item, settings, document_url=acq.url, digest=digest)
    decision, existing = classify(feed=ctx.feed, mailbox=ctx.mailbox, key=key, digest=digest,
                                  version_on_change=settings["dedupe"].get("version_on_change", True))
    base = {"key": key, "url": item.source_url or acq.url, "file_type": file_type, "flags": list(item.quality_flags),
            "matched_keywords": [m["term"] for m in matched], "extra_fields": dict(item.fields),
            "body_preview": (item.body_text or item.raw_text)[:500], "rule_id": item.rule_id,
            "published_at": item.published_at, "published_at_text": item.published_at_text,
            "attention": list(item.attention)}
    if decision in {"unchanged", "changed_ignored"}:
        ctx.stats[decision] += 1
        if not ctx.dry_run:
            _sighting(existing)
        if decision == "changed_ignored":
            ctx.log("info", f"Content changed but versioning is off for this feed: {item.title[:120]}")
        result = _result(item.title, decision, message_id=existing.pk, **base)
        ctx.results.append(result)
        return result

    if item.attention:
        status = "needs_attention"
    elif not keep:
        status = "filtered_out"
    elif decision == "duplicate":
        status = "duplicate"
    elif decision == "updated":
        status = "updated"
    else:
        status = "new"
    if acq.force_status:
        status = acq.force_status
    ctx.stats[status] += 1
    reason_text = "\n".join(item.attention) if item.attention else (reason if not keep or flag else "")
    if ctx.dry_run:
        result = _result(item.title, status, decision=decision, reason=reason_text, **base)
        ctx.results.append(result)
        return result

    flags = list(item.quality_flags)
    values = {
        "title": item.title, "body_text": item.body_text, "body_html": item.body_html,
        "raw_extracted_text": item.raw_text,
        "search_text": search_normalize(f"{item.title}\n{item.body_text}\n{' '.join(map(str, item.fields.values()))}"),
    }
    _fit_packet(ctx, values, flags)
    message = CollectedMessage(
        source=ctx.feed.source if ctx.feed else None, feed=ctx.feed, mailbox=ctx.mailbox, raw_artifact=artifact,
        fetch_run=ctx.run, source_url=(item.source_url or acq.url)[:2000], source_item_key=key,
        detected_language=item.meta.get("detected_language", ""), published_at_text=item.published_at_text[:300],
        published_at=item.published_at, content_hash=digest, status=status, attention_reason=reason_text[:5000],
        matched_keywords=_jsonable(matched), keyword_score=score, extra_fields=_jsonable(item.fields),
        extraction_method=(item.method or "")[:100], extracting_rule_id=item.rule_id,
        extraction_confidence=item.confidence, quality_flags=flags, manual_entry=acq.manual,
        created_by=ctx.user if acq.manual else None, **values,
    )
    with transaction.atomic():
        if decision == "updated":
            previous = CollectedMessage.objects.select_for_update().get(pk=existing.pk)
            previous.is_latest = False
            previous.save(update_fields=["is_latest"])
            message.version = previous.version + 1
            message.supersedes = previous
        elif decision == "duplicate":
            message.duplicate_of = existing
        message.save()
    if status in {"new", "updated"}:
        ctx.stats["new_or_updated"] += 1
    result = _result(item.title, status, message_id=message.pk, decision=decision, reason=reason_text, **base)
    ctx.results.append(result)
    return result


def save_failure(ctx, acq, artifact, file_type, reason, rule=None):
    """An artifact that could not be turned into messages still becomes a visible message."""
    digest = artifact.sha256 if artifact is not None else sha256_hex(acq.data)
    key = (acq.overrides.get("source_item_key") or acq.url or f"hash:{digest}")[:512]
    title = acq.overrides.get("title") or acq.hints.get("title") or acq.link_text or acq.filename or acq.url or "Untitled"
    ctx.stats["extraction_failed"] += 1
    decision, existing = classify(feed=ctx.feed, mailbox=ctx.mailbox, key=key, digest=digest)
    if decision == "unchanged":
        if not ctx.dry_run:
            _sighting(existing)
        return _result(title, "unchanged", message_id=existing.pk, reason=reason)
    if ctx.dry_run:
        result = _result(title, "extraction_failed", reason=reason, file_type=file_type, url=acq.url)
        ctx.results.append(result)
        return result
    with transaction.atomic():
        message = CollectedMessage(
            source=ctx.feed.source if ctx.feed else None, feed=ctx.feed, mailbox=ctx.mailbox, raw_artifact=artifact,
            fetch_run=ctx.run, source_url=acq.url[:2000], source_item_key=key, title=title[:1000], content_hash=digest,
            status="extraction_failed", attention_reason=reason[:5000], extracting_rule=rule,
            extraction_method=f"parser:{file_type}", quality_flags=[], manual_entry=acq.manual,
            created_by=ctx.user if acq.manual else None,
        )
        if decision == "updated":
            previous = CollectedMessage.objects.select_for_update().get(pk=existing.pk)
            previous.is_latest = False
            previous.save(update_fields=["is_latest"])
            message.version = previous.version + 1
            message.supersedes = previous
        message.save()
    result = _result(title, "extraction_failed", message_id=message.pk, reason=reason, file_type=file_type, url=acq.url)
    ctx.results.append(result)
    return result


# --- Documents -----------------------------------------------------------------------
def _apply_values(items, values, *, only_empty):
    for item in items:
        for name, value in values.items():
            if value in (None, ""):
                continue
            if name == "published_at":
                if not only_empty or item.published_at is None:
                    item.published_at = value
            elif name == "fields":
                for field_name, field_value in value.items():
                    if not only_empty or field_name not in item.fields:
                        item.fields[field_name] = field_value
            elif not only_empty or not item.get(name):
                item.set(name, value)


def _archive(ctx, acq, file_type):
    if ctx.dry_run:
        return None
    artifact, created = store_artifact(
        acq.data, file_type=file_type, filename=acq.filename, content_type=acq.content_type, source_url=acq.url,
        headers=acq.headers, origin=acq.origin, parent=acq.parent, feed=ctx.feed, run=ctx.run,
    )
    ctx.stats["artifacts_new" if created else "artifacts_seen"] += 1
    return artifact


def process_document(ctx, acq, depth=0):
    """Run one acquired document through the whole pipeline. Returns a list of result dicts."""
    files = ctx.settings["files"]
    file_type = detect_file_type(acq.data, acq.filename, acq.content_type, acq.url)
    label = acq.filename or acq.url or "pasted text"
    ctx.stats["documents"] += 1

    problem = None
    if len(acq.data) < float(files.get("min_size_kb", 0)) * 1024:
        problem = f"smaller than the minimum size ({files['min_size_kb']} KB)"
    elif len(acq.data) > float(files.get("max_size_mb", 50)) * 1024 * 1024:
        problem = f"larger than the maximum size ({files['max_size_mb']} MB)"
    elif file_type not in files.get("allowed_types", []) and not acq.bypass_type_check:
        if acq.manual:  # an operator chose this file on purpose: process it, but say so
            ctx.log("info", f"{label}: type '{file_type}' is not in this feed's allowed types; processed because it was uploaded manually.")
        else:
            problem = f"file type '{file_type}' is not allowed for this feed"
    if problem:
        action = files.get("disallowed_action", "ignore")
        if acq.manual and action == "ignore":
            action = "flag"  # an operator's upload is never ignored silently
        ctx.stats["disallowed"] += 1
        if action == "ignore":
            ctx.log("info", f"Ignored {label}: {problem}", url=acq.url, file_type=file_type)
            return [_result(label, "ignored", reason=problem, file_type=file_type, url=acq.url)]
        artifact = _archive(ctx, acq, file_type)
        if action == "store_raw":
            ctx.log("info", f"Stored raw file only ({problem}): {label}", url=acq.url)
            return [_result(label, "stored_raw", reason=problem, file_type=file_type, url=acq.url)]
        item = Item(title=acq.overrides.get("title") or acq.hints.get("title") or acq.link_text or acq.filename,
                    source_url=acq.url, link_text=acq.link_text, method="not_processed")
        item.flag("unparsed_file_type")
        item.attention.append(f"Not processed: {problem}")
        return [save_item(ctx, item, ParsedDocument(file_type=file_type), artifact, acq, file_type)]

    artifact = _archive(ctx, acq, file_type)
    try:
        parser_cls = parser_for(file_type)
        if parser_cls is None:
            raise ValueError(f"no parser for file type '{file_type}'")
        options, _rule = parse_options(ctx.rules, file_type, acq.url)
        parser = parser_cls()
        parser.file_type_hint = file_type
        document = parser.parse(acq.data, filename=acq.filename, content_type=acq.content_type, url=acq.url,
                                options=options, settings=ctx.settings)
    except Exception as exc:  # noqa: BLE001 - any parser failure is recorded, never lost
        ctx.log("error", f"Could not read {label} as {file_type}: {type(exc).__name__}: {exc}", url=acq.url)
        return [save_failure(ctx, acq, artifact, file_type, f"Parse error ({file_type}): {type(exc).__name__}: {exc}")]
    for warning in document.warnings:
        ctx.log("warning", f"{label}: {warning}", url=acq.url)

    if file_type == "zip" and acq.process_children:  # the archive itself is not a message; its files are
        return _process_children(ctx, acq, document, artifact, file_type, depth)
    results = _process_items(ctx, acq, document, artifact, file_type, label)
    if not acq.process_children:
        return results
    return results + _process_children(ctx, acq, document, artifact, file_type, depth)


def _process_children(ctx, acq, document, artifact, file_type, depth):
    """ZIP members and email attachments, each processed as its own document (after the parent)."""
    results = []
    if not document.attachments or depth >= 2:
        return results
    for attachment in document.attachments:
        if file_type == "eml" and attachment.get("inline") and attachment["content_type"].startswith("image/"):
            ctx.log("debug", f"Skipped inline image {attachment['filename']}")
            continue
        child = Acquired(
            data=attachment["data"], filename=attachment["filename"], content_type=attachment["content_type"],
            url=f"{acq.url}#{attachment['filename']}" if acq.url else "",
            origin="attachment" if file_type == "eml" else "archive", parent=artifact, link_text=acq.link_text,
            hints={k: v for k, v in acq.hints.items() if k in {"published_at", "published_at_text"}},
            overrides={k: v for k, v in acq.overrides.items() if k in {"published_at"}}, manual=acq.manual,
        )
        results.extend(process_document(ctx, child, depth + 1))
    return results


def _process_items(ctx, acq, document, artifact, file_type, label):
    results = []
    items = default_items(document, url=acq.url, link_text=acq.link_text, filename=acq.filename)
    appended = "\n\n".join(f"[{name}]\n{text}" for name, text in acq.append_texts if text)
    for item in items:
        item.meta.update({f"http.{k.lower()}": v for k, v in (acq.headers or {}).items()})
        for flag in acq.flags:
            item.flag(flag)
        item.attention.extend(acq.attention)
        if appended:
            item.body_text = f"{item.body_text}\n\n{appended}".strip()
    _apply_values(items, acq.hints, only_empty=True)
    try:
        items = apply_rules(document, items, ctx.rules, settings_for=settings_resolver(ctx.settings), url=acq.url,
                            file_type=file_type, log=ctx.log, feed=ctx.feed)
        items = extract_custom_fields(items, ctx.custom_fields, document=document, url=acq.url, file_type=file_type,
                                      settings=ctx.settings, log=ctx.log, feed=ctx.feed)
    except RuleError as exc:
        ctx.log("error", f"{label}: {exc}", url=acq.url, rule=exc.rule.pk)
        return results + [save_failure(ctx, acq, artifact, file_type, str(exc), rule=exc.rule)]
    _apply_values(items, acq.overrides, only_empty=False)
    if not items:
        ctx.log("warning", f"{label}: extraction rules produced no items", url=acq.url)
        return results + [save_failure(ctx, acq, artifact, file_type, "Extraction rules produced no items")]
    for item in items:
        if ctx.limit_reached:
            ctx.log("info", f"Max items per run ({ctx.feed.max_items_per_run}) reached; remaining items skipped.")
            ctx.stats["skipped_limit"] += 1
            break
        results.append(save_item(ctx, item, document, artifact, acq, file_type))
    return results


# --- Entry points --------------------------------------------------------------------
def _update_feed_state(feed, ctx):
    now = timezone.now()
    values = {"last_run_at": now, "first_run_done": True, "last_extraction_stats": dict(ctx.stats)}
    if ctx.status in {"success", "partial"}:
        values.update(last_success_at=now, consecutive_failures=0)
    if ctx.status in {"failed", "partial"}:
        errors = [e["message"] for e in ctx.events if e["level"] == "error"]
        values.update(last_error_at=now, last_error="\n".join(errors)[:5000])
    if ctx.status == "failed":
        values["consecutive_failures"] = F("consecutive_failures") + 1
    if ctx.stats.get("new_or_updated"):
        values["last_new_message_at"] = now
    if ctx.status == "success":
        values["last_error"] = ""
    Feed.objects.filter(pk=feed.pk).update(**values)
    try:
        from .health import refresh_feed_health
    except ImportError:
        return
    refresh_feed_health(Feed.objects.get(pk=feed.pk))


def run_feed(feed, *, trigger="manual", user=None, dry_run=False, worker=""):
    """Run a feed's acquisition method and process everything it yields. Returns the RunContext."""
    owner = locks.new_owner(trigger)
    if not dry_run and not locks.acquire(Feed, feed.pk, owner):
        run = FetchRun.objects.create(feed=feed, trigger=trigger, status="skipped", finished_at=timezone.now(),
                                      triggered_by=user if getattr(user, "is_authenticated", False) else None,
                                      error_message="Skipped: another run of this feed is still in progress.")
        ctx = RunContext.__new__(RunContext)
        ctx.run, ctx.status, ctx.stats, ctx.results, ctx.events = run, "skipped", Counter(), [], []
        return ctx
    ctx = RunContext(feed, trigger=trigger, user=user, dry_run=dry_run, worker=worker or owner)
    error = ""
    try:
        fetcher = registry.get("fetcher", feed.method)(effective_method_config(feed))
        ctx.log("info", f"Run started: {fetcher.label} ({'dry run, nothing saved' if dry_run else trigger})")
        for acq in fetcher.collect(ctx):
            try:
                process_document(ctx, acq)
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
                ctx.log("error", f"Unexpected error processing {acq.url or acq.filename}: {exc}",
                        traceback=traceback.format_exc(limit=5))
            if ctx.limit_reached:
                break
            if not dry_run:
                locks.extend(Feed, feed.pk, owner)
    except PluginNotFound as exc:
        ctx.fatal, error = True, str(exc)
        ctx.log("error", error)
    except NotImplementedError:
        ctx.fatal, error = True, f"Acquisition method '{feed.method}' cannot be run (use upload / mailbox for it)."
        ctx.log("error", error)
    except Exception as exc:  # noqa: BLE001 - recorded on the run and the feed
        ctx.fatal, error = True, f"Run failed: {type(exc).__name__}: {exc}"
        ctx.log("error", error, traceback=traceback.format_exc(limit=8))
    finally:
        ctx.log("info", f"Run finished: {dict(ctx.stats)}")
        ctx.finish(error=error)
        if not dry_run:
            _update_feed_state(feed, ctx)
            locks.release(Feed, feed.pk, owner)
    return ctx


def ingest_upload(feed, *, user=None, files=(), text="", title="", url="", published_at=None, language="",
                  dry_run=False):
    """Manual upload / paste. ``files`` = [(filename, bytes, content_type)]. Returns the RunContext."""
    settings = effective_settings(feed)
    if language:
        settings = copy.deepcopy(settings)
        settings["content"]["language"] = language
    ctx = RunContext(feed, trigger="upload", user=user, dry_run=dry_run, settings=settings, honour_limits=False)
    overrides = {"published_at": published_at,
                 "published_at_text": published_at.isoformat() if published_at else ""}
    single = (len(files) + (1 if text.strip() else 0)) == 1
    if title and single:
        overrides["title"] = title
    if url:
        overrides["source_url"] = url
    try:
        for filename, data, content_type in files:
            ctx.log("info", f"Uploaded file {filename} ({len(data)} bytes)")
            process_document(ctx, Acquired(data=data, filename=filename, content_type=content_type, url=url,
                                           origin="upload", overrides=overrides, manual=True))
        if text.strip():
            ctx.log("info", f"Pasted text ({len(text)} characters)")
            process_document(ctx, Acquired(data=text.encode("utf-8"), filename="pasted-text.txt",
                                           content_type="text/plain; charset=utf-8", url=url, origin="paste",
                                           overrides=overrides, manual=True))
    except Exception as exc:  # noqa: BLE001
        ctx.fatal = True
        ctx.log("error", f"Upload failed: {type(exc).__name__}: {exc}", traceback=traceback.format_exc(limit=8))
    ctx.finish()
    if not dry_run and ctx.stats.get("new_or_updated"):
        Feed.objects.filter(pk=feed.pk).update(last_new_message_at=timezone.now())
    return ctx
