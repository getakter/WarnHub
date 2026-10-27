"""Mailbox polling, routing and delivery.

For every new email (new = not yet seen by UID/UIDVALIDITY or Message-ID):
  1. the original .eml is archived as a RawArtifact (always, even if nothing matches)
  2. routing rules are evaluated in order (stop at the first match unless the rule says otherwise)
  3. each matching rule delivers body and/or attachments to its target feed, through the normal pipeline
  4. no match -> one message with status 'unrouted' (never dropped silently)
  5. the post-action runs (mark read / move / label / leave), then the email is recorded as seen
"""
import email
import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

from django.db.models import Q
from django.utils import timezone

from apps.acquisition.base import Acquired
from apps.collection import locks
from apps.collection.archive import store_artifact
from apps.collection.models import FetchRun, FetchRunEvent
from apps.collection.pipeline import RunContext, process_document
from apps.core.bangla import clean_text, nfc
from apps.extraction.filetypes import detect_file_type
from apps.extraction.parsers import html_to_text, parser_for
from apps.plugins.registry import registry
from apps.sources.config import effective_settings

from .models import MailboxAccount, MailboxSeenMessage
from .protocols import MailboxError

logger = logging.getLogger("ewce.mailbox")


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes
    inline: bool
    file_type: str

    @property
    def size(self):
        return len(self.data)


@dataclass
class EmailInfo:
    subject: str = ""
    from_address: str = ""
    to_addresses: list = field(default_factory=list)
    message_id: str = ""
    date: object = None
    body_text: str = ""
    attachments: list = field(default_factory=list)


def parse_email(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)

    def header(name):
        try:
            value = msg.get(name)
            return clean_text(str(value)) if value is not None else ""
        except Exception:  # noqa: BLE001 - malformed headers
            return ""

    info = EmailInfo(subject=header("subject"), message_id=header("message-id").strip("<>"))
    froms = getaddresses([header("from")])
    info.from_address = (froms[0][1] if froms else "").lower()
    info.to_addresses = [addr.lower() for _name, addr in getaddresses([header("to"), header("cc")]) if addr]
    try:
        info.date = parsedate_to_datetime(header("date")) if header("date") else None
    except (TypeError, ValueError):
        info.date = None
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, UnicodeDecodeError):
            content = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        info.body_text = html_to_text(content) if body.get_content_type() == "text/html" else clean_text(content)
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True)
        if data is None:
            continue
        filename = part.get_filename() or ""
        disposition = (part.get_content_disposition() or "").lower()
        inline = disposition == "inline" or (bool(part.get("Content-ID")) and disposition != "attachment")
        info.attachments.append(Attachment(filename=filename, content_type=part.get_content_type(), data=data,
                                           inline=inline, file_type=detect_file_type(data, filename, part.get_content_type())))
    return info


# --- Routing -------------------------------------------------------------------------
def address_matches(pattern, address):
    """'name@agency.gov.bd' exact, '@agency.gov.bd' or 'agency.gov.bd' = that domain and its subdomains."""
    pattern, address = pattern.strip().lower(), (address or "").lower()
    if not pattern or not address:
        return False
    if "@" in pattern and not pattern.startswith("@"):
        return address == pattern
    domain = pattern.lstrip("@")
    host = address.rsplit("@", 1)[-1]
    return host == domain or host.endswith("." + domain)


def _contains_any(phrases, text):
    haystack = nfc(text or "").casefold()
    return [p for p in phrases if nfc(p).casefold() in haystack]


def evaluate_rule(rule, info):
    """Return (matched, [(condition, ok, detail)]). Only configured conditions count."""
    checks = []
    if rule.from_patterns:
        hits = [p for p in rule.from_patterns if address_matches(p, info.from_address)]
        checks.append(("from", bool(hits), f"{info.from_address or '(none)'} vs {', '.join(rule.from_patterns)}"))
    if rule.to_patterns:
        hits = [p for p in rule.to_patterns for a in info.to_addresses if address_matches(p, a)]
        checks.append(("to", bool(hits), f"{', '.join(info.to_addresses) or '(none)'} vs {', '.join(rule.to_patterns)}"))
    if rule.subject_contains:
        hits = _contains_any(rule.subject_contains, info.subject)
        checks.append(("subject contains", bool(hits), f"found: {', '.join(hits)}" if hits else "none of the phrases"))
    if rule.subject_regex:
        ok = bool(re.search(rule.subject_regex, info.subject or "", re.IGNORECASE))
        checks.append(("subject regex", ok, rule.subject_regex))
    if rule.body_keywords:
        hits = _contains_any(rule.body_keywords, info.body_text)
        checks.append(("body keywords", bool(hits), f"found: {', '.join(hits)}" if hits else "none found"))
    real_attachments = [a for a in info.attachments if not a.inline]
    if rule.has_attachment == "yes":
        checks.append(("has attachment", bool(real_attachments), f"{len(real_attachments)} attachment(s)"))
    elif rule.has_attachment == "no":
        checks.append(("has no attachment", not real_attachments, f"{len(real_attachments)} attachment(s)"))
    if rule.attachment_types:
        types = sorted({a.file_type for a in real_attachments})
        ok = any(t in rule.attachment_types for t in types)
        checks.append(("attachment types", ok, f"{', '.join(types) or 'none'} vs {', '.join(rule.attachment_types)}"))
    if not checks:
        return True, [("(no conditions)", True, "matches every email")]
    matched = all(ok for _c, ok, _d in checks) if rule.match_mode == "all" else any(ok for _c, ok, _d in checks)
    return matched, checks


def route(rules, info):
    """Matching rules in order, honouring stop_processing. Returns [(rule, checks)]."""
    matches = []
    for rule in rules:
        if not rule.enabled:
            continue
        matched, checks = evaluate_rule(rule, info)
        if matched:
            matches.append((rule, checks))
            if rule.stop_processing:
                break
    return matches


def select_attachments(rule, info, feed_settings):
    allowed = rule.allowed_attachment_types or feed_settings["files"].get("allowed_types", [])
    chosen, skipped = [], []
    for att in info.attachments:
        if rule.ignore_inline_images and att.inline and att.file_type == "image":
            skipped.append((att, "inline image / signature logo"))
        elif att.file_type not in allowed:
            skipped.append((att, f"type '{att.file_type}' not allowed"))
        elif att.size > rule.max_attachment_mb * 1024 * 1024:
            skipped.append((att, f"larger than {rule.max_attachment_mb:g} MB"))
        else:
            chosen.append(att)
    return chosen, skipped


# --- Delivery ------------------------------------------------------------------------
def _attachment_text(att, ctx):
    from apps.extraction.engine import parse_options

    parser_cls = parser_for(att.file_type)
    if parser_cls is None:
        return f"(attachment {att.filename}: no parser for {att.file_type})"
    try:
        parser = parser_cls()
        parser.file_type_hint = att.file_type
        options, _ = parse_options(ctx.rules, att.file_type, "")
        return parser.parse(att.data, filename=att.filename, content_type=att.content_type, options=options,
                            settings=ctx.settings).text
    except Exception as exc:  # noqa: BLE001 - recorded in the body instead of failing the email
        return f"(attachment {att.filename} could not be read: {exc})"


def deliver(rule, info, raw, eml_artifact, ctx):
    """Send the parts of one email that the rule asks for to the rule's feed."""
    key_base = f"email:{info.message_id}" if info.message_id else ""
    common = {"published_at": info.date}
    take_body = rule.take in {"body", "both"}
    take_attachments = rule.take in {"attachments", "both"}
    chosen, skipped = select_attachments(rule, info, ctx.settings) if take_attachments else ([], [])
    for att, reason in skipped:
        ctx.log("info", f"Attachment {att.filename or att.content_type} skipped: {reason}")
    attention = []
    if rule.take == "attachments" and not chosen:  # nothing to collect: keep the email body visible instead
        attention.append("The routing rule expects attachments, but none were usable "
                         + (f"({'; '.join(f'{a.filename}: {r}' for a, r in skipped)})" if skipped else "(no attachments)"))
    body_acq = dict(data=raw, filename=f"{info.message_id or 'email'}.eml"[:200], content_type="message/rfc822",
                    origin="email", process_children=False, bypass_type_check=True,
                    hints={**common, "title": info.subject, "source_item_key": key_base}, attention=attention)
    if rule.split == "per_email":
        texts = [(f"Attachment: {a.filename}", _attachment_text(a, ctx)) for a in chosen]
        process_document(ctx, Acquired(**body_acq, append_texts=texts))
        return
    if take_body or attention:
        process_document(ctx, Acquired(**body_acq))
    for att in chosen:
        process_document(ctx, Acquired(
            data=att.data, filename=att.filename, content_type=att.content_type, origin="attachment",
            parent=eml_artifact, bypass_type_check=True,
            hints={**common, "title": f"{info.subject} — {att.filename}" if info.subject else att.filename,
                   "source_item_key": f"{key_base}#{att.filename}" if key_base else ""},
        ))


def _apply_post_action(conn, folder, uid, action, target, log):
    try:
        if action == "mark_read":
            conn.mark_read(folder, uid)
        elif action == "move" and target:
            conn.move(folder, uid, target)
        elif action == "label" and target:
            conn.label(folder, uid, target)
        return True
    except Exception as exc:  # noqa: BLE001 - the email itself is already safely collected
        log("warning", f"Post-action '{action}' failed for UID {uid}: {exc}")
        return False


class _RunLog:
    def __init__(self, run):
        self.run = run
        self.errors = 0

    def __call__(self, level, message, **context):
        if level == "error":
            self.errors += 1
        getattr(logger, "warning" if level == "warning" else "error" if level == "error" else "info")(
            "mailbox_run=%s %s", self.run.pk, message)
        FetchRunEvent.objects.create(run=self.run, level=level, message=message[:5000], context=context)


def process_email(account, conn, folder, uid, uidvalidity, raw, rules, run, log, stats):
    info = parse_email(raw)
    stats["emails"] = stats.get("emails", 0) + 1
    label = f"'{info.subject[:80]}' from {info.from_address}"
    if info.message_id and MailboxSeenMessage.objects.filter(mailbox=account, message_id=info.message_id).exists():
        MailboxSeenMessage.objects.create(mailbox=account, folder=folder, uid=uid, uidvalidity=uidvalidity,
                                          message_id=info.message_id[:500], subject=info.subject[:1000],
                                          from_address=info.from_address[:500], received_at=info.date,
                                          outcome="skipped", detail="Same Message-ID already processed")
        stats["already_seen"] = stats.get("already_seen", 0) + 1
        return "skipped"
    eml_artifact, _ = store_artifact(raw, file_type="eml", filename=f"{folder}-{uid}.eml"[:200],
                                     content_type="message/rfc822", origin="email", run=run)
    matches = route(rules, info)
    if matches:
        for rule, _checks in matches:
            ctx = RunContext(rule.target_feed, trigger="mailbox", mailbox=account, run=run, honour_limits=False)
            log("info", f"Email {label} → rule '{rule.name}' → feed {rule.target_feed}")
            deliver(rule, info, raw, eml_artifact, ctx)
            _merge(stats, ctx.stats)
        outcome, first = "routed", matches[0][0]
        action = first.post_action if first.post_action != "default" else account.default_post_action
        target = first.post_target if first.post_action != "default" else account.default_post_target
    else:
        log("info", f"Email {label} matched no rule → saved as unrouted")
        ctx = RunContext(None, trigger="mailbox", mailbox=account, run=run, honour_limits=False)
        process_document(ctx, Acquired(data=raw, filename=f"{folder}-{uid}.eml", content_type="message/rfc822",
                                       origin="email", process_children=False, bypass_type_check=True,
                                       force_status="unrouted",
                                       hints={"title": info.subject, "published_at": info.date,
                                              "source_item_key": f"email:{info.message_id}" if info.message_id else ""}))
        _merge(stats, ctx.stats)
        outcome, first = "unrouted", None
        action, target = account.unmatched_post_action, account.default_post_target
    stats[f"emails_{outcome}"] = stats.get(f"emails_{outcome}", 0) + 1  # message statuses are counted separately
    MailboxSeenMessage.objects.create(mailbox=account, folder=folder, uid=uid, uidvalidity=uidvalidity,
                                      message_id=info.message_id[:500], subject=info.subject[:1000],
                                      from_address=info.from_address[:500], received_at=info.date, rule=first,
                                      raw_artifact=eml_artifact, outcome=outcome)
    if action and action != "none":
        _apply_post_action(conn, folder, uid, action, target, log)
    return outcome


def _merge(stats, other):
    for key, value in other.items():
        stats[key] = stats.get(key, 0) + value


def poll_mailbox(account, *, trigger="mailbox", user=None, worker=""):
    """Fetch and process new emails. Returns the FetchRun."""
    owner = locks.new_owner("mailbox")
    if not locks.acquire(MailboxAccount, account.pk, owner):
        return FetchRun.objects.create(mailbox=account, trigger=trigger, status="skipped", finished_at=timezone.now(),
                                       error_message="Skipped: this mailbox is already being polled.")
    run = FetchRun.objects.create(mailbox=account, trigger=trigger, worker=(worker or owner)[:100],
                                  triggered_by=user if getattr(user, "is_authenticated", False) else None)
    log, stats, fatal = _RunLog(run), {}, ""
    conn = None
    try:
        conn = registry.get("mailbox_protocol", account.protocol)(account.options).connect(account)
        rules = list(account.rules.filter(enabled=True).select_related("target_feed__source"))
        if not rules:
            log("warning", "This mailbox has no enabled routing rules; every email will be saved as unrouted.")
        remaining = account.max_messages_per_run
        since = ((account.last_success_at - timedelta(days=1)) if account.last_success_at
                 else timezone.now() - timedelta(days=account.initial_lookback_days)).date()
        for folder in account.watched_folders():
            uidvalidity = conn.uidvalidity(folder)
            uids = [str(u) for u in conn.search_uids(folder, since=since, unseen_only=account.only_unseen)]
            done = set(MailboxSeenMessage.objects.filter(mailbox=account, folder=folder, uidvalidity=uidvalidity)
                       .values_list("uid", flat=True))
            new = [u for u in uids if u not in done][:remaining]
            log("info", f"{folder}: {len(uids)} email(s) since {since}, {len(new)} new")
            raws = conn.fetch_raw(folder, new)
            for uid in new:
                if uid not in raws:
                    log("error", f"{folder} UID {uid}: could not download the message")
                    continue
                try:
                    process_email(account, conn, folder, uid, uidvalidity, raws[uid], rules, run, log, stats)
                except Exception as exc:  # noqa: BLE001 - one bad email must not stop the poll
                    log("error", f"{folder} UID {uid}: {type(exc).__name__}: {exc}", traceback=traceback.format_exc(limit=5))
            remaining -= len(new)
            if remaining <= 0:
                log("info", f"Max messages per run ({account.max_messages_per_run}) reached.")
                break
    except MailboxError as exc:
        fatal = str(exc)
        log("error", fatal)
    except Exception as exc:  # noqa: BLE001 - recorded on the run and the mailbox
        fatal = f"{type(exc).__name__}: {exc}"
        log("error", fatal, traceback=traceback.format_exc(limit=8))
    finally:
        if conn is not None:
            conn.close()
        now = timezone.now()
        status = "failed" if fatal else ("partial" if log.errors else "success")
        FetchRun.objects.filter(pk=run.pk).update(status=status, finished_at=now, stats=stats,
                                                  error_message=fatal or "")
        values = {"last_polled_at": now, "next_poll_at": now + timedelta(minutes=max(1, account.poll_interval_minutes))}
        if status != "failed":
            values.update(last_success_at=now, last_error="")
        else:
            values["last_error"] = fatal[:2000]
        MailboxAccount.objects.filter(pk=account.pk).update(**values)
        locks.release(MailboxAccount, account.pk, owner)
    run.refresh_from_db()
    return run


def poll_due_mailboxes(worker="", now=None):
    now = now or timezone.now()
    due = MailboxAccount.objects.filter(is_active=True).filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
    return [poll_mailbox(account, worker=worker) for account in due]


# --- Read-only helpers for the UI ----------------------------------------------------
def test_connection(account):
    """Return (ok, message, details) without changing anything on the server."""
    try:
        conn = registry.get("mailbox_protocol", account.protocol)(account.options).connect(account)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc), []
    try:
        available = conn.folders()
        details = []
        for folder in account.watched_folders():
            if folder not in available:
                details.append((folder, f"not found on the server (available: {', '.join(available[:15])})"))
            else:
                counts = conn.counts(folder)
                details.append((folder, f"{counts['messages']} messages, {counts['unseen']} unread"))
        return True, f"Logged in to {account.host} as {account.username}.", details
    finally:
        conn.close()


def preview_inbox(account, limit=20):
    """Recent emails (read-only) with the rule that would match each. Returns (rows, error)."""
    try:
        conn = registry.get("mailbox_protocol", account.protocol)(account.options).connect(account)
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    rules = list(account.rules.filter(enabled=True).select_related("target_feed__source"))
    rows = []
    try:
        for folder in account.watched_folders():
            uidvalidity = conn.uidvalidity(folder)
            uids = [str(u) for u in conn.search_uids(folder)][-limit:]
            seen = dict(MailboxSeenMessage.objects.filter(mailbox=account, folder=folder, uidvalidity=uidvalidity,
                                                          uid__in=uids).values_list("uid", "outcome"))
            for uid, raw in sorted(conn.fetch_raw(folder, uids).items(), key=lambda kv: int(kv[0]), reverse=True):
                info = parse_email(raw)
                rows.append({"folder": folder, "uid": uid, "info": info, "matches": route(rules, info),
                             "processed": seen.get(uid)})
    finally:
        conn.close()
    return rows, ""


def explain(account, raw):
    """Rule tester: every rule's verdict for one email, plus what the first match would collect."""
    info = parse_email(raw)
    verdicts = []
    for rule in account.rules.select_related("target_feed__source"):
        matched, checks = evaluate_rule(rule, info)
        verdicts.append({"rule": rule, "matched": matched and rule.enabled, "checks": checks})
    matches = route(list(account.rules.select_related("target_feed__source")), info)
    plans = []
    for rule, _checks in matches:
        chosen, skipped = select_attachments(rule, info, effective_settings(rule.target_feed)) if rule.take != "body" else ([], [])
        plans.append({"rule": rule, "chosen": chosen, "skipped": skipped})
    return info, verdicts, plans
