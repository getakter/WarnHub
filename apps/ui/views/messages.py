"""Collected messages: list/search/export, detail, attention marking, raw artifacts, upload, run log."""
import csv
import datetime as dt
import json

from django.contrib import messages as flash
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.collection.models import CollectedMessage, FetchRun, RawArtifact
from apps.collection.pipeline import ingest_upload
from apps.core.audit import audit
from apps.core.bangla import nfc, search_normalize
from apps.core.roles import operator_required

from ..forms import AttentionForm, MessageFilterForm, UploadForm

PAGE_SIZE = 50


def filtered_messages(params):
    """Apply the message filter form to a queryset. Returns (form, queryset)."""
    form = MessageFilterForm(params or None)
    qs = CollectedMessage.objects.select_related("source", "feed", "raw_artifact", "mailbox")
    if not form.is_valid():
        return form, qs
    data = form.cleaned_data
    if data["q"]:
        for word in search_normalize(data["q"]).split():
            qs = qs.filter(search_text__contains=word)
    if data["source"]:
        qs = qs.filter(source=data["source"])
    if data["feed"]:
        qs = qs.filter(feed=data["feed"])
    if data["status"]:
        qs = qs.filter(status__in=data["status"])
    if data["language"]:
        qs = qs.filter(detected_language=data["language"])
    field = data["date_field"] or "collected_at"
    tz = timezone.get_current_timezone()
    if data["date_from"]:
        qs = qs.filter(**{f"{field}__gte": dt.datetime.combine(data["date_from"], dt.time.min, tzinfo=tz)})
    if data["date_to"]:
        qs = qs.filter(**{f"{field}__lt": dt.datetime.combine(data["date_to"] + dt.timedelta(days=1), dt.time.min, tzinfo=tz)})
    if data["keyword"]:
        qs = qs.filter(matched_keywords__contains=[{"term": nfc(data["keyword"].strip())}])
    if data["flag"]:
        qs = qs.filter(quality_flags__contains=[data["flag"]])
    if data["field_name"]:
        if data["field_value"]:
            qs = qs.filter(**{f"extra_fields__{data['field_name']}__icontains": data["field_value"]})
        else:
            qs = qs.filter(**{f"extra_fields__has_key": data["field_name"]})
    if data["latest_only"]:
        qs = qs.filter(is_latest=True)
    if data["manual"] == "yes":
        qs = qs.filter(manual_entry=True)
    elif data["manual"] == "no":
        qs = qs.filter(manual_entry=False)
    if data["run"]:
        qs = qs.filter(fetch_run_id=data["run"])
    return form, qs


@operator_required
def message_list(request):
    form, qs = filtered_messages(request.GET)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "ui/messages/list.html", {
        "form": form, "page": page, "total": page.paginator.count, "query": query.urlencode(),
    })


class _Echo:
    def write(self, value):
        return value


CSV_COLUMNS = [
    "id", "collected_at", "published_at", "published_at_text", "source", "feed", "status", "version", "is_latest",
    "title", "source_url", "source_item_key", "detected_language", "matched_keywords", "quality_flags",
    "extra_fields", "attention_reason", "manual_entry", "raw_artifact_sha256", "body_text",
]


@operator_required
def message_export_csv(request):
    _form, qs = filtered_messages(request.GET)
    writer = csv.writer(_Echo())
    local = timezone.get_current_timezone()

    def rows():
        yield "﻿"  # BOM so Excel shows Bangla correctly
        yield writer.writerow(CSV_COLUMNS)
        for m in qs.iterator(chunk_size=500):
            yield writer.writerow([
                m.pk, timezone.localtime(m.collected_at, local).strftime("%Y-%m-%d %H:%M"),
                timezone.localtime(m.published_at, local).strftime("%Y-%m-%d %H:%M") if m.published_at else "",
                m.published_at_text, m.source.code if m.source else "", m.feed.name if m.feed else "", m.status,
                m.version, m.is_latest, m.title, m.source_url, m.source_item_key, m.detected_language,
                "; ".join(k.get("term", "") for k in m.matched_keywords), "; ".join(m.quality_flags),
                json.dumps(m.extra_fields, ensure_ascii=False), m.attention_reason, m.manual_entry,
                m.raw_artifact.sha256 if m.raw_artifact else "", m.body_text,
            ])

    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="messages-{timezone.localdate():%Y%m%d}.csv"'
    return response


@operator_required
def message_detail(request, pk):
    message = get_object_or_404(
        CollectedMessage.objects.select_related("source", "feed", "raw_artifact", "fetch_run", "extracting_rule",
                                                "supersedes", "duplicate_of", "created_by", "mailbox"), pk=pk)
    artifact = message.raw_artifact
    return render(request, "ui/messages/detail.html", {
        "message": message,
        "artifact": artifact,
        "versions": message.version_chain(),
        "duplicates": message.duplicates.all()[:20],
        "attention_form": AttentionForm(),
        "preview_kind": _preview_kind(artifact),
        "parent_artifact": artifact.parent if artifact else None,
    })


def _preview_kind(artifact):
    if artifact is None:
        return None
    if artifact.file_type in {"html", "eml", "txt", "csv"}:
        return "frame"
    if artifact.file_type == "pdf":
        return "pdf"
    if artifact.file_type == "image":
        return "image"
    return "download"


@operator_required
@require_POST
def message_mark_attention(request, pk):
    message = get_object_or_404(CollectedMessage, pk=pk)
    form = AttentionForm(request.POST)
    if form.is_valid():
        note = f"[{timezone.localtime():%Y-%m-%d %H:%M} {request.user.get_username()}] {form.cleaned_data['reason']}"
        message.attention_reason = f"{message.attention_reason}\n{note}".strip()[:5000]
        previous = message.status
        message.status = "needs_attention"
        message.save(update_fields=["status", "attention_reason"])
        audit("mark_needs_attention", message, previous_status=previous, reason=form.cleaned_data["reason"])
        flash.success(request, "Marked as needs attention.")
    return redirect(message.get_absolute_url())


@operator_required
@require_POST
def message_clear_attention(request, pk):
    message = get_object_or_404(CollectedMessage, pk=pk, status="needs_attention")
    message.status = "updated" if message.version > 1 else "new"
    note = f"[{timezone.localtime():%Y-%m-%d %H:%M} {request.user.get_username()}] attention cleared"
    message.attention_reason = f"{message.attention_reason}\n{note}".strip()[:5000]
    message.save(update_fields=["status", "attention_reason"])
    audit("clear_needs_attention", message, new_status=message.status)
    flash.success(request, f"Attention cleared; status is now {message.status}.")
    return redirect(message.get_absolute_url())


INLINE_TYPES = {"html": "text/html", "eml": "text/plain; charset=utf-8", "txt": "text/plain; charset=utf-8",
                "csv": "text/plain; charset=utf-8", "pdf": "application/pdf"}


@operator_required
@xframe_options_sameorigin
def artifact_view(request, pk):
    """Serve an original. HTML snapshots are sandboxed so their scripts never run in our origin."""
    artifact = get_object_or_404(RawArtifact, pk=pk)
    if not artifact.path.exists():
        raise Http404("The archived file is missing from disk.")
    download = request.GET.get("download") == "1" or artifact.file_type not in INLINE_TYPES | {"image": ""}
    if artifact.file_type == "html":
        charset = ""
        ctype = artifact.content_type or ""
        if "charset=" in ctype.lower():
            charset = "; " + ctype.split(";", 1)[1].strip()
        content_type = "text/html" + charset
    elif artifact.file_type == "image":
        content_type = artifact.content_type if artifact.content_type.startswith("image/") else "application/octet-stream"
    else:
        content_type = INLINE_TYPES.get(artifact.file_type, "application/octet-stream")
    filename = artifact.original_filename or f"{artifact.sha256[:16]}{artifact.path.suffix}"
    response = FileResponse(open(artifact.path, "rb"), content_type=content_type, as_attachment=download,
                            filename=filename)
    response["X-Content-Type-Options"] = "nosniff"
    if artifact.file_type in {"html", "eml", "txt", "csv"}:
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src * data:; style-src 'unsafe-inline' *"
    return response


@operator_required
def upload(request):
    initial = {"feed": request.GET.get("feed")}
    form = UploadForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        files = [(f.name, f.read(), f.content_type or "") for f in data["files"]]
        ctx = ingest_upload(
            data["feed"], user=request.user, files=files, text=data["text"] or "", title=data["title"] or "",
            url=data["url"] or "", published_at=data["published_at"], language=data["language"] or "",
        )
        audit("upload", data["feed"], run=ctx.run.pk, files=[f[0] for f in files], pasted=bool(data["text"]))
        summary = ", ".join(f"{n} {k}" for k, n in ctx.stats.items() if k in {
            "new", "updated", "duplicate", "unchanged", "filtered_out", "needs_attention", "extraction_failed"})
        level = flash.success if ctx.status == "success" else flash.warning
        level(request, f"Upload processed ({ctx.status}): {summary or 'nothing saved'}.")
        return redirect(ctx.run.get_absolute_url())
    return render(request, "ui/upload.html", {"form": form})


@operator_required
def run_list(request):
    runs = FetchRun.objects.select_related("feed__source", "mailbox", "triggered_by")
    status = request.GET.get("status")
    if status:
        runs = runs.filter(status=status)
    trigger = request.GET.get("trigger")
    if trigger:
        runs = runs.filter(trigger=trigger)
    if request.GET.get("feed"):
        runs = runs.filter(feed_id=request.GET["feed"])
    page = Paginator(runs, PAGE_SIZE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "ui/runs/list.html", {"page": page, "status": status, "statuses": FetchRun.STATUSES,
                                                 "trigger": trigger, "triggers": FetchRun.TRIGGERS,
                                                 "query": query.urlencode()})


@operator_required
def run_detail(request, pk):
    run = get_object_or_404(FetchRun.objects.select_related("feed__source", "mailbox", "triggered_by"), pk=pk)
    level = request.GET.get("level")
    events = run.events.all()
    if level:
        events = events.filter(level=level)
    return render(request, "ui/runs/detail.html", {
        "run": run, "events": events, "level": level,
        "messages_created": run.messages.select_related("feed").order_by("id")[:200],
        "message_count": run.messages.count(),
        "stats": sorted((run.stats or {}).items()),
    })
