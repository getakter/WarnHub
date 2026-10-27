"""Dry-run helpers behind 'Test rule', 'Test field', 'Test on history' and 'Test feed'.

Everything runs through the real pipeline with dry_run=True, so tests show exactly what a real run
would produce (and, against stored history, whether the result would be unchanged or a new version).
"""
from apps.acquisition.base import Acquired
from apps.acquisition.http import PoliteClient
from apps.sources.config import effective_settings

from .archive import read_artifact
from .models import CollectedMessage, RawArtifact
from .pipeline import RunContext, process_document

PARSE_PHASE = {"pdf", "office", "email"}


def rules_with(feed, edited=None, *, upto=True):
    """Feed rules with ``edited`` swapped in (or appended); item rules after it are dropped when upto."""
    rules = list(feed.rules.all())
    if edited is None:
        return rules
    if edited.pk and any(r.pk == edited.pk for r in rules):
        rules = [edited if r.pk == edited.pk else r for r in rules]
    else:
        rules.append(edited)
        rules.sort(key=lambda r: (r.position, r.pk or 10**9))
    if not upto:
        return rules
    index = next(i for i, r in enumerate(rules) if r is edited)
    return rules[: index + 1] + [r for r in rules[index + 1:] if r.rule_type in PARSE_PHASE]


def fields_with(feed, edited=None):
    fields = list(feed.custom_fields.all())
    if edited is None:
        return fields
    if edited.pk and any(f.pk == edited.pk for f in fields):
        return [edited if f.pk == edited.pk else f for f in fields]
    return fields + [edited]


def acquire_url(feed, url, log=None):
    client = PoliteClient(effective_settings(feed)["fetch"], log=log)
    result = client.get(url)
    return Acquired(data=result.content, url=result.url, content_type=result.content_type, headers=result.headers,
                    flags=list(result.flags))


def acquire_artifact(artifact):
    return Acquired(data=read_artifact(artifact), url=artifact.source_url, filename=artifact.original_filename,
                    content_type=artifact.content_type, headers=artifact.response_headers, parent=artifact.parent)


def recent_artifacts(feed, limit=5):
    ids = (CollectedMessage.objects.filter(feed=feed, raw_artifact__isnull=False, raw_artifact__parent__isnull=True)
           .order_by("-collected_at").values_list("raw_artifact_id", flat=True))
    unique = list(dict.fromkeys(ids))[:limit]
    by_id = RawArtifact.objects.in_bulk(unique)
    return [by_id[i] for i in unique if i in by_id]


def dry_run(feed, acquired, *, rules=None, fields=None):
    """Process one document without saving anything. Returns the RunContext (results + events)."""
    ctx = RunContext(feed, trigger="test", dry_run=True, honour_limits=False)
    if rules is not None:
        ctx.rules = rules
    if fields is not None:
        ctx.custom_fields = fields
    try:
        process_document(ctx, acquired)
    except Exception as exc:  # noqa: BLE001 - shown in the test panel
        ctx.log("error", f"{type(exc).__name__}: {exc}")
    ctx.finish()
    return ctx


def test_on_history(feed, *, rules=None, fields=None, limit=5):
    """Re-extract the last N stored artifacts with the edited config and compare to what is stored."""
    report = []
    for artifact in recent_artifacts(feed, limit):
        try:
            acquired = acquire_artifact(artifact)
        except OSError as exc:
            report.append({"artifact": artifact, "error": f"Archived file unreadable: {exc}", "results": [],
                           "stored": []})
            continue
        ctx = dry_run(feed, acquired, rules=rules, fields=fields)
        stored = list(CollectedMessage.objects.filter(feed=feed, raw_artifact=artifact, is_latest=True)
                      .exclude(status="duplicate").order_by("id"))
        changed = sum(1 for r in ctx.results if r["status"] not in {"unchanged", "changed_ignored"})
        report.append({"artifact": artifact, "results": ctx.results, "events": ctx.events, "stored": stored,
                       "changed": changed, "error": ""})
    return report
