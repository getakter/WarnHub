"""Configuration import / export and 'Run in background'."""
import json

from django.conf import settings
from django.contrib import messages as flash
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.collection.models import WorkerHeartbeat
from apps.core.audit import audit
from apps.core.roles import admin_required, operator_required
from apps.mailbox.models import MailboxAccount
from apps.sources.models import Feed, FormatProfile, KeywordSet, Source
from apps.sources.portability import export_all, export_config, import_config

from ..forms import ImportConfigForm
from .common import json_download


def _slug():
    return slugify(settings.EWCE_PRODUCT_NAME) or "config"


EXPORTABLE = {
    "source": (Source, "sources"),
    "feed": (Feed, "feeds"),
    "profile": (FormatProfile, "profiles"),
    "keywordset": (KeywordSet, "keyword_sets"),
    "mailbox": (MailboxAccount, "mailboxes"),
}


@operator_required
def export_everything(request):
    data = export_all(user=request.user)
    audit("export_config", None, scope="all")
    return json_download(data, f"{_slug()}-config-{timezone.localdate():%Y%m%d}.json")


@operator_required
def export_object(request, kind, pk):
    if kind not in EXPORTABLE:
        flash.error(request, f"Cannot export {kind}.")
        return redirect("ui:config_io")
    model, section = EXPORTABLE[kind]
    obj = get_object_or_404(model, pk=pk)
    selection = {section: [obj]}
    if kind == "source":
        selection["feeds"] = list(obj.feeds.select_related("source"))
    data = export_config(**selection, user=request.user, note=f"Export of {kind} {obj}")
    audit("export_config", obj, scope=kind)
    slug = getattr(obj, "code", None) or getattr(obj, "slug", None) or str(obj.pk)
    return json_download(data, f"{_slug()}-{kind}-{slug}-{timezone.localdate():%Y%m%d}.json")


@admin_required
def config_io(request):
    form = ImportConfigForm(request.POST or None, request.FILES or None)
    report, raw = None, ""
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        raw = upload.read().decode("utf-8-sig") if upload else form.cleaned_data["text"]
        try:
            data = json.loads(raw or "")
        except json.JSONDecodeError as exc:
            form.add_error(None, f"Not valid JSON: {exc}")
        else:
            report = import_config(data, on_conflict=form.cleaned_data["on_conflict"],
                                   dry_run=form.cleaned_data["dry_run"], user=request.user)
            if report.applied:
                flash.success(request, f"Imported: {report.counts()}")
            elif report.ok and report.dry_run:
                flash.info(request, "Dry run only: nothing was changed. Untick 'Dry run' to apply.")
            else:
                flash.error(request, "Import failed; nothing was changed.")
    return render(request, "ui/config_io.html", {
        "form": form, "report": report,
        "counts": {"sources": Source.objects.count(), "profiles": FormatProfile.objects.count(),
                   "keyword_sets": KeywordSet.objects.count(), "feeds": Feed.objects.count(),
                   "mailboxes": MailboxAccount.objects.count()},
    })


@operator_required
@require_POST
def feed_queue_run(request, pk):
    feed = get_object_or_404(Feed, pk=pk)
    Feed.objects.filter(pk=pk).update(run_requested_at=timezone.now())
    audit("queue_run", feed)
    alive = any(w.alive for w in WorkerHeartbeat.objects.all())
    if alive:
        flash.success(request, f"{feed} is queued; the background worker will start it within a minute. See Runs.")
    else:
        flash.warning(request, f"{feed} is queued, but no worker is running. Start one with: "
                               "python manage.py run_worker  (or use Run now).")
    return redirect(feed.get_absolute_url())
