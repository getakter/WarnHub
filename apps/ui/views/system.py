"""Dashboard, plugin catalogue, version history/rollback and small JSON helpers."""
import json

from django.apps import apps as django_apps
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.collection.models import CollectedMessage, FetchRun
from apps.core.audit import audit
from apps.core.dbcheck import database_warnings
from apps.core.models import AuditLog, ConfigVersion
from apps.core.roles import admin_required, operator_required
from apps.core.versioning import rollback
from apps.mailbox.models import MailboxAccount
from apps.plugins.registry import KINDS, registry
from apps.plugins.schema_forms import SchemaForm
from apps.plugins.validation import FORMAT_MESSAGES, format_checker
from apps.sources.models import Feed, FormatProfile, KeywordSet, Source


PROBLEM_HEALTH = ["failing", "silent", "degraded"]


@operator_required
def dashboard(request):
    from datetime import timedelta

    from django.db.models import Prefetch
    from django.utils import timezone

    from apps.collection.health import refresh_all_health
    from apps.collection.models import FetchRunEvent, WorkerHeartbeat

    workers = list(WorkerHeartbeat.objects.all()[:5])
    if not any(w.alive for w in workers):
        refresh_all_health()  # no worker keeping health fresh: compute it now
    status_counts = dict(CollectedMessage.objects.values_list("status").annotate(n=Count("id")).order_by())
    feeds = Feed.objects.select_related("source")
    health_counts = dict(feeds.values_list("health_status").annotate(n=Count("id")).order_by())
    since = timezone.now() - timedelta(hours=24)
    return render(request, "ui/dashboard.html", {
        "problem_feeds": feeds.filter(health_status__in=PROBLEM_HEALTH).order_by("health_status", "source__name"),
        "sources": Source.objects.prefetch_related(
            Prefetch("feeds", queryset=Feed.objects.order_by("-is_active", "name"))),
        "workers": workers,
        "worker_alive": any(w.alive for w in workers),
        "recent_errors": FetchRunEvent.objects.filter(level="error", created_at__gte=since)
        .select_related("run__feed__source", "run__mailbox").order_by("-created_at")[:10],
        "mailboxes": MailboxAccount.objects.all(),
        "unrouted": status_counts.get("unrouted", 0),
        "needs_attention": status_counts.get("needs_attention", 0) + status_counts.get("extraction_failed", 0),
        "optional": optional_components(),
        "counts": {
            "sources": Source.objects.count(),
            "feeds": feeds.count(),
            "active_feeds": feeds.filter(is_active=True).count(),
            "profiles": FormatProfile.objects.count(),
            "keyword_sets": KeywordSet.objects.count(),
            "mailboxes": MailboxAccount.objects.count(),
            "messages": sum(status_counts.values()),
        },
        "status_counts": [(code, label, status_counts.get(code, 0)) for code, label in CollectedMessage.STATUSES],
        "health_counts": [(code, label, health_counts.get(code, 0)) for code, label in Feed.HEALTH_CHOICES],
        "feeds": feeds.order_by("-is_active", "source__name", "name")[:50],
        "recent_runs": FetchRun.objects.select_related("feed", "mailbox")[:8],
        "db_warnings": database_warnings(),
        "plugin_errors": registry.load_errors,
        "recent_audit": AuditLog.objects.select_related("user")[:8],
    })


@operator_required
def plugin_catalogue(request):
    kinds = []
    for kind, label in KINDS.items():
        plugins = []
        for name, cls in registry.all(kind).items():
            schema = cls.schema()
            plugins.append({
                "cls": cls, "info": cls.info(),
                "form": SchemaForm(schema, prefix=f"{kind}-{name}") if schema.get("properties") else None,
                "schema_json": json.dumps(schema, indent=2, ensure_ascii=False),
            })
        kinds.append({"kind": kind, "label": label, "plugins": plugins})
    return render(request, "ui/plugins.html", {"kinds": kinds, "load_errors": registry.load_errors,
                                               "optional": optional_components()})


def optional_components():
    from apps.acquisition.http import js_rendering_available
    from apps.extraction.ocr import ocr_available, ocr_status_message

    return {"ocr": ocr_available(), "ocr_message": ocr_status_message(), "js": js_rendering_available()}


@require_POST
@operator_required
def validate_pattern(request):
    """Syntax check for regex / CSS selector / XPath fields (the 'Check' button next to them)."""
    fmt = request.POST.get("format", "")
    value = request.POST.get("value", "")
    values = [v for v in value.splitlines() if v.strip()] if request.POST.get("lines") else [value]
    if fmt not in FORMAT_MESSAGES:
        return JsonResponse({"ok": False, "error": f"Unknown format {fmt}"}, status=400)
    for item in values:
        try:
            format_checker.check(item, fmt)
        except Exception as exc:  # noqa: BLE001 - message goes back to the form
            cause = exc.cause if getattr(exc, "cause", None) else exc
            return JsonResponse({"ok": False, "error": f"{item!r} {FORMAT_MESSAGES[fmt]} ({cause})"})
    return JsonResponse({"ok": True, "message": f"Valid {fmt.replace('-', ' ')}"})


# --- Version history ------------------------------------------------------------------
VERSIONED_MODELS = {
    "sources.source", "sources.feed", "sources.formatprofile", "sources.extractionrule",
    "sources.customfielddefinition", "sources.keywordset", "mailbox.mailboxaccount", "mailbox.mailboxrule",
}


def _resolve(model_label, pk):
    if model_label not in VERSIONED_MODELS:
        raise Http404("Not a versioned object")
    model = django_apps.get_model(model_label)
    return model, model.objects.filter(pk=pk).first()


def _diff(old, new):
    rows = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            rows.append((key, old.get(key), new.get(key)))
    return rows


@operator_required
def version_list(request, model_label, pk):
    model, obj = _resolve(model_label, pk)
    ct = ContentType.objects.get_for_model(model)
    versions = list(ConfigVersion.objects.filter(content_type=ct, object_id=pk).select_related("created_by"))
    previous = {}
    for version in reversed(versions):
        version.changes = _diff(previous, version.snapshot) if previous else []
        previous = version.snapshot
    return render(request, "ui/versions/list.html", {
        "object": obj, "model_label": model_label, "pk": pk, "versions": versions,
        "verbose_name": model._meta.verbose_name,
    })


@admin_required
@require_POST
def version_rollback(request, version_pk):
    version = get_object_or_404(ConfigVersion, pk=version_pk)
    obj = version.content_type.model_class().objects.filter(pk=version.object_id).first()
    if obj is None:
        messages.error(request, "The object no longer exists; export the snapshot JSON and re-import it instead.")
        return redirect(request.POST.get("next") or "ui:dashboard")
    try:
        rollback(obj, version)
    except Exception as exc:  # noqa: BLE001 - validation errors are shown to the admin
        messages.error(request, f"Rollback failed: {'; '.join(getattr(exc, 'messages', [str(exc)]))}")
    else:
        audit("rollback", obj, to_version=version.version)
        messages.success(request, f"Rolled {obj} back to version {version.version}.")
    return redirect(request.POST.get("next") or obj.get_absolute_url())


@operator_required
def audit_log(request):
    entries = AuditLog.objects.select_related("user")[:300]
    return render(request, "ui/audit.html", {"entries": entries})
