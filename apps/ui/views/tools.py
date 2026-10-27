"""Inspect URL, selector playground, rule/field tests, Test feed (dry run) and Run now."""
import uuid

from django.contrib import messages as flash
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.acquisition.base import Acquired
from apps.acquisition.crawl import DOCUMENT_TYPES, guess_type, in_scope
from apps.acquisition.http import FetchError, PoliteClient
from apps.collection import testing
from apps.collection.pipeline import run_feed
from apps.core.audit import audit
from apps.core.roles import admin_required, operator_required
from apps.extraction import select as sel
from apps.extraction.filetypes import detect_file_type
from apps.extraction.inspect import preview_page, rule_selector, test_regex, test_selector
from apps.extraction.parsers import parser_for
from apps.plugins.registry import PluginNotFound, registry
from apps.plugins.schema_forms import SchemaForm
from apps.sources.config import builtin_defaults, effective_settings
from apps.sources.models import CustomFieldDefinition, ExtractionRule, Feed
from apps.sources.settings_schema import SETTINGS_SCHEMA

from ..forms import CustomFieldForm, RuleForm

CACHE_TTL = 30 * 60
TEXT_PREVIEW_CHARS = 8000


def _cache_key(token):
    return f"ewce:inspect:{token}"


@operator_required
def inspect_url(request):
    url = request.GET.get("url", "").strip()
    feed = Feed.objects.filter(pk=request.GET.get("feed") or None).select_related("source").first()
    context = {"url": url, "feed": feed, "feeds": Feed.objects.select_related("source"), "use_js": bool(request.GET.get("js"))}
    if not url:
        return render(request, "ui/tools/inspect.html", context)
    settings = effective_settings(feed) if feed else builtin_defaults()
    fetch = dict(settings["fetch"])
    if context["use_js"]:
        fetch["requires_js"] = True
    events = []
    try:
        result = PoliteClient(fetch, log=lambda level, message, **_: events.append((level, message))).get(url)
    except FetchError as exc:
        context["error"] = str(exc)
        return render(request, "ui/tools/inspect.html", context)
    file_type = detect_file_type(result.content, "", result.content_type, result.url)
    document, parse_error = None, ""
    parser_cls = parser_for(file_type)
    if parser_cls is not None:
        try:
            parser = parser_cls()
            parser.file_type_hint = file_type
            document = parser.parse(result.content, content_type=result.content_type, url=result.url, settings=settings)
        except Exception as exc:  # noqa: BLE001 - shown on the page
            parse_error = f"{type(exc).__name__}: {exc}"
    html = document.html if document is not None else ""
    text = document.text if document is not None else ""
    token = uuid.uuid4().hex
    cache.set(_cache_key(token), {"url": result.url, "html": html, "text": text}, CACHE_TTL)
    links = []
    for link in (document.links if document is not None else []):
        kind = guess_type(link["url"])
        ok, reason = in_scope(link["url"], link["text"], settings["scope"], result.url) if feed else (None, "")
        links.append({**link, "type": kind or "page", "is_document": kind in DOCUMENT_TYPES, "in_scope": ok,
                      "reason": reason})
    links.sort(key=lambda link: (not link["is_document"],))
    root = sel.parse(html)
    context.update({
        "result": result, "file_type": file_type, "document": document, "parse_error": parse_error, "token": token,
        "text_preview": text[:TEXT_PREVIEW_CHARS], "text_length": len(text), "links": links[:500],
        "link_count": len(links), "document_links": sum(1 for link in links if link["is_document"]),
        "suggestions": sel.suggest_listing_selectors(root) if root is not None else [],
        "preview": preview_page(html, result.url) if html else "", "events": events,
        "headers": sorted(result.headers.items()),
    })
    return render(request, "ui/tools/inspect.html", context)


@operator_required
@require_POST
def playground(request):
    data = cache.get(_cache_key(request.POST.get("token", "")))
    if data is None:
        return JsonResponse({"error": "This inspection expired; inspect the URL again."}, status=410)
    kind, expression = request.POST.get("kind", "css"), request.POST.get("expression", "").strip()
    if not expression:
        return JsonResponse({"error": "Type a selector or pattern first."}, status=400)
    if kind == "regex":
        return JsonResponse(test_regex(data["text"], expression))
    if not data["html"]:
        return JsonResponse({"error": "This document is not HTML; use a regex on its text instead."}, status=400)
    result = test_selector(data["html"], data["url"], css=expression if kind == "css" else "",
                           xpath=expression if kind == "xpath" else "")
    return JsonResponse(result)


# --- Rule / field tests --------------------------------------------------------------
def _edited_rule(request, feed):
    pk = request.POST.get("_test_object_pk")
    rule = get_object_or_404(ExtractionRule, pk=pk, feed=feed) if pk else ExtractionRule(feed=feed)
    if not pk:
        rule.rule_type = request.POST.get("rule_type", "")
        rule.position = (feed.rules.order_by("-position").values_list("position", flat=True).first() or 0) + 10
    plugin = registry.get("extractor", rule.rule_type)
    cform = SchemaForm(plugin.schema(), rule.config, data=request.POST, prefix="c")
    oform = SchemaForm(SETTINGS_SCHEMA, rule.settings_override, inherit=True, data=request.POST, prefix="o")
    errors = []
    if cform.is_valid() & oform.is_valid():
        rule.config, rule.settings_override = cform.config, oform.config
    else:
        errors += [f"{name}: {', '.join(e)}" for name, e in {**cform.errors, **oform.errors}.items()]
    form = RuleForm(request.POST, instance=rule)
    if not form.is_valid():
        errors += [f"{name}: {', '.join(e)}" for name, e in form.errors.items()]
    rule.enabled = True  # a disabled rule is still tested
    return rule, errors


def _edited_field(request, feed):
    pk = request.POST.get("_test_object_pk")
    field = get_object_or_404(CustomFieldDefinition, pk=pk, feed=feed) if pk else CustomFieldDefinition(feed=feed)
    if not pk:
        field.extractor = request.POST.get("extractor", "regex")
    plugin = registry.get("field_extractor", field.extractor)
    cform = SchemaForm(plugin.schema(), field.config, data=request.POST, prefix="c")
    errors = []
    if cform.is_valid():
        field.config = cform.config
    else:
        errors += [f"{name}: {', '.join(e)}" for name, e in cform.errors.items()]
    form = CustomFieldForm(request.POST, instance=field)
    if not form.is_valid():
        errors += [f"{name}: {', '.join(e)}" for name, e in form.errors.items() if name not in {"name", "__all__"}]
    field.enabled = True
    if not field.name:
        field.name = "tested_field"
    return field, errors


@admin_required
@require_POST
def test_extraction(request, feed_pk):
    feed = get_object_or_404(Feed, pk=feed_pk)
    target, mode = request.POST.get("_test_target", "rule"), request.POST.get("_test_mode", "url")
    context = {"feed": feed, "mode": mode, "target": target}
    try:
        if target == "field":
            edited, errors = _edited_field(request, feed)
            rules, fields = testing.rules_with(feed), testing.fields_with(feed, edited)
        else:
            edited, errors = _edited_rule(request, feed)
            rules, fields = testing.rules_with(feed, edited), None
    except PluginNotFound as exc:
        return render(request, "ui/tools/_test_results.html", {**context, "errors": [str(exc)]})
    context["errors"] = errors
    if errors:
        return render(request, "ui/tools/_test_results.html", context)
    if mode == "history":
        limit = max(1, min(int(request.POST.get("_test_limit") or 5), 20))
        context["history"] = testing.test_on_history(feed, rules=rules, fields=fields, limit=limit)
        return render(request, "ui/tools/_test_results.html", context)
    try:
        if mode == "file":
            upload = request.FILES.get("_test_file")
            if upload is None:
                raise FetchError("Choose a file to test with.")
            acquired = Acquired(data=upload.read(), filename=upload.name, content_type=upload.content_type or "",
                                url=request.POST.get("_test_url", "").strip())
        else:
            url = request.POST.get("_test_url", "").strip()
            if not url:
                raise FetchError("Enter a URL to test with.")
            acquired = testing.acquire_url(feed, url)
    except FetchError as exc:
        context["errors"] = [str(exc)]
        return render(request, "ui/tools/_test_results.html", context)
    ctx = testing.dry_run(feed, acquired, rules=rules, fields=fields)
    context.update({"results": ctx.results, "events": ctx.events, "stats": sorted(ctx.stats.items()),
                    "has_failures": bool(ctx.stats.get("extraction_failed") or ctx.error_count)})
    if target == "rule":
        css, xpath = rule_selector(edited.rule_type, edited.config)
        if (css or xpath) and detect_file_type(acquired.data, acquired.filename, acquired.content_type, acquired.url) == "html":
            from apps.extraction.textdecode import decode_bytes

            html, _ = decode_bytes(acquired.data, content_type=acquired.content_type)
            context["highlight"] = test_selector(html, acquired.url, css=css, xpath=xpath)
    return render(request, "ui/tools/_test_results.html", context)


# --- Feed: test (dry run) and run now ------------------------------------------------
def _runnable(feed):
    try:
        return getattr(registry.get("fetcher", feed.method), "schedulable", True)
    except PluginNotFound:
        return False


@operator_required
@require_POST
def feed_test(request, pk):
    feed = get_object_or_404(Feed.objects.select_related("source"), pk=pk)
    if not _runnable(feed):
        flash.error(request, f"'{feed.method}' feeds receive items from uploads or mailboxes; they cannot be run.")
        return redirect(feed.get_absolute_url())
    ctx = run_feed(feed, trigger="test", user=request.user, dry_run=True)
    audit("test_feed", feed, status=ctx.status, stats=dict(ctx.stats))
    return render(request, "ui/feeds/test_results.html", {
        "feed": feed, "results": ctx.results, "events": ctx.events, "stats": sorted(ctx.stats.items()),
        "status": ctx.status,
    })


@operator_required
@require_POST
def feed_run_now(request, pk):
    feed = get_object_or_404(Feed, pk=pk)
    if not _runnable(feed):
        flash.error(request, f"'{feed.method}' feeds receive items from uploads or mailboxes; they cannot be run.")
        return redirect(feed.get_absolute_url())
    ctx = run_feed(feed, trigger="manual", user=request.user)
    audit("run_now", feed, run=ctx.run.pk, status=ctx.status)
    level = {"success": flash.success, "partial": flash.warning}.get(ctx.status, flash.error)
    level(request, f"Run finished: {ctx.status}. " + ", ".join(f"{k} {v}" for k, v in sorted(ctx.stats.items())))
    return redirect(ctx.run.get_absolute_url())
