"""Configuration screens: sources, feeds, rules, custom fields, profiles, keyword sets."""
import csv
import io

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.roles import admin_required, operator_required
from apps.core.versioning import record_version, version_context, versions_for
from apps.plugins.registry import PluginNotFound, registry
from apps.plugins.schema_forms import SchemaForm
from apps.sources import services
from apps.sources.config import effective_method_config, effective_settings, provenance_rows
from apps.sources.models import (
    CustomFieldDefinition, ExtractionRule, Feed, FeedKeywordSet, FormatProfile, Keyword, KeywordSet, Source,
)
from apps.sources.settings_schema import SETTINGS_SCHEMA

from ..forms import (
    CustomFieldForm, FeedCreateForm, FeedForm, FeedKeywordSetsForm, KeywordCsvImportForm, KeywordForm,
    KeywordSetForm, ProfileForm, RuleForm, SourceForm,
)
from .common import delete_with_confirm, render_form


def _settings_form(request, current, effective, prefix="s"):
    data = request.POST if request.method == "POST" else None
    return SchemaForm(SETTINGS_SCHEMA, current, inherit=True, effective=effective, data=data, prefix=prefix)


def _plugin_form(request, kind, name, current, prefix):
    try:
        plugin = registry.get(kind, name)
    except PluginNotFound:
        return None, None
    data = request.POST if request.method == "POST" else None
    return plugin, SchemaForm(plugin.schema(), current, data=data, prefix=prefix)


# --- Sources -------------------------------------------------------------------------
@operator_required
def source_list(request):
    sources = Source.objects.annotate(
        feed_count=Count("feeds", distinct=True),
        active_feeds=Count("feeds", filter=Q(feeds__is_active=True), distinct=True),
    )
    return render(request, "ui/sources/list.html", {"sources": sources})


@operator_required
def source_detail(request, pk):
    source = get_object_or_404(Source, pk=pk)
    return render(request, "ui/sources/detail.html", {
        "source": source, "feeds": source.feeds.select_related("profile"),
        "settings_rows": provenance_rows(source.settings_layer(), {}),
    })


@admin_required
def source_edit(request, pk=None):
    source = get_object_or_404(Source, pk=pk) if pk else Source()
    form = SourceForm(request.POST or None, request.FILES or None, instance=source)
    sform = _settings_form(request, source.settings, effective_settings_defaults())
    if request.method == "POST" and sform.is_valid():
        source.settings = sform.config
        if form.is_valid():
            form.save()
            audit("update" if pk else "create", source)
            messages.success(request, f"Saved {source}.")
            return redirect(source.get_absolute_url())
    return render_form(
        request, form, title=f"Edit {source}" if pk else "New source",
        back_url=source.get_absolute_url() if pk else reverse("ui:source_list"),
        schema_forms=[("Default settings for all feeds of this source", sform)],
        enctype="multipart/form-data",
    )


def effective_settings_defaults():
    from apps.sources.config import builtin_defaults

    return builtin_defaults()


@admin_required
def source_delete(request, pk):
    source = get_object_or_404(Source, pk=pk)
    return delete_with_confirm(
        request, source, back_url=source.get_absolute_url(), success_url=reverse("ui:source_list"),
        warning="Feeds must be deleted first. Sources with collected messages cannot be deleted; disable them.",
    )


@admin_required
@require_POST
def source_clone(request, pk):
    source = get_object_or_404(Source, pk=pk)
    with version_context(change_type="clone", comment=f"Cloned from source #{source.pk}"):
        copy = Source.objects.get(pk=pk)
        copy.pk = None
        copy._state.adding = True
        copy.name = services.unique_value(Source, "name", f"{source.name} (copy)", 200)
        copy.code = services.unique_value(Source, "code", f"{source.code}-copy", 40)
        copy.is_example = False
        copy.save()
    audit("clone", copy, cloned_from=source.pk)
    messages.success(request, f"Cloned as {copy}. Feeds are not copied; clone them individually.")
    return redirect(reverse("ui:source_edit", args=[copy.pk]))


@admin_required
@require_POST
def toggle_active(request, model, pk):
    model_cls = {"source": Source, "feed": Feed, "profile": FormatProfile, "keywordset": KeywordSet}[model]
    obj = get_object_or_404(model_cls, pk=pk)
    obj.is_active = not obj.is_active
    if model == "feed" and obj.is_active:
        try:
            obj.full_clean()
        except Exception as exc:  # noqa: BLE001 - surfaced to the admin
            messages.error(request, f"Cannot enable {obj}: {'; '.join(getattr(exc, 'messages', [str(exc)]))}")
            return redirect(request.POST.get("next") or obj.get_absolute_url())
    obj.save()
    audit("enable" if obj.is_active else "disable", obj)
    messages.success(request, f"{obj} is now {'enabled' if obj.is_active else 'disabled'}.")
    return redirect(request.POST.get("next") or obj.get_absolute_url())


# --- Feeds ---------------------------------------------------------------------------
@operator_required
def feed_list(request):
    feeds = Feed.objects.select_related("source", "profile").annotate(
        rule_count=Count("rules", distinct=True), message_count=Count("messages", distinct=True)
    )
    source_id = request.GET.get("source")
    if source_id:
        feeds = feeds.filter(source_id=source_id)
    return render(request, "ui/feeds/list.html", {"feeds": feeds, "sources": Source.objects.all(),
                                                  "source_id": source_id})


@operator_required
def feed_detail(request, pk):
    feed = get_object_or_404(Feed.objects.select_related("source", "profile"), pk=pk)
    merged, provenance = effective_settings(feed, with_provenance=True)
    try:
        method_plugin = registry.get("fetcher", feed.method)
        method_config = effective_method_config(feed)
    except PluginNotFound:
        method_plugin, method_config = None, {}
    return render(request, "ui/feeds/detail.html", {
        "feed": feed,
        "rules": feed.rules.all(),
        "custom_fields": feed.custom_fields.all(),
        "keyword_links": FeedKeywordSet.objects.filter(feed=feed).select_related("keyword_set"),
        "effective_rows": provenance_rows(merged, provenance),
        "capture_mode": merged.get("capture", {}).get("mode"),
        "runnable": bool(method_plugin and getattr(method_plugin, "schedulable", True)),
        "method_plugin": method_plugin,
        "method_config": method_config,
        "versions": versions_for(feed)[:10],
        "rule_plugins": registry.all("extractor"),
        "field_plugins": registry.all("field_extractor"),
        "recent_runs": feed.runs.all()[:5],
    })


@admin_required
def feed_create(request):
    initial = {"source": request.GET.get("source")}
    form = FeedCreateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["profile"]:
            feed = services.create_feed_from_profile(data["source"], data["profile"], data["name"])
        else:
            from django.utils.text import slugify

            feed = Feed(source=data["source"], name=data["name"], method=data["method"] or "web_page",
                        slug=services.unique_value(Feed, "slug", slugify(data["name"]) or "feed", 100,
                                                   source=data["source"]))
            feed.full_clean()
            feed.save()
            audit("create", feed)
        messages.success(request, f"Created {feed} (disabled). Configure entry URLs, rules and schedule, test, then enable.")
        return redirect(reverse("ui:feed_edit", args=[feed.pk]))
    return render_form(request, form, title="New feed", back_url=reverse("ui:feed_list"), submit="Create")


@admin_required
def feed_edit(request, pk):
    feed = get_object_or_404(Feed.objects.select_related("source", "profile"), pk=pk)
    method = request.POST.get("method") or request.GET.get("method") or feed.method
    method_current = feed.method_config if method == feed.method else {}
    form = FeedForm(request.POST or None, instance=feed, initial={"method": method})
    lower = effective_settings(feed, upto="feed")
    sform = _settings_form(request, feed.settings, lower)
    plugin, mform = _plugin_form(request, "fetcher", method, method_current, "m")
    schema_forms = []
    if mform is not None and mform.schema.get("properties"):
        schema_forms.append((f"{plugin.label} options", mform))
    schema_forms.append(("Settings (blank = inherit from source / profile)", sform))
    if request.method == "POST":
        valid = sform.is_valid() & (mform is None or mform.is_valid())
        if valid:
            feed.settings = sform.config
            feed.method_config = mform.config if mform is not None else {}
            if form.is_valid():
                form.save()
                audit("update", feed)
                messages.success(request, f"Saved {feed}.")
                return redirect(feed.get_absolute_url())
        messages.error(request, "Please fix the errors below.")
    return render_form(
        request, form, title=f"Edit feed: {feed}", back_url=feed.get_absolute_url(),
        schema_forms=schema_forms, method_help=plugin.description if plugin else "",
    )


@admin_required
def feed_delete(request, pk):
    feed = get_object_or_404(Feed, pk=pk)
    return delete_with_confirm(request, feed, back_url=feed.get_absolute_url(), success_url=reverse("ui:feed_list"),
                               warning="Feeds with collected messages cannot be deleted; disable them instead.")


@admin_required
@require_POST
def feed_clone(request, pk):
    copy = services.clone_feed(get_object_or_404(Feed, pk=pk))
    messages.success(request, f"Cloned as {copy} (disabled).")
    return redirect(reverse("ui:feed_edit", args=[copy.pk]))


@admin_required
@require_POST
def feed_save_as_profile(request, pk):
    feed = get_object_or_404(Feed, pk=pk)
    profile = services.save_feed_as_profile(feed, request.POST.get("name") or f"Profile from {feed.name}")
    messages.success(request, f"Saved format profile {profile}.")
    return redirect(profile.get_absolute_url())


@admin_required
def feed_keyword_sets(request, pk):
    feed = get_object_or_404(Feed, pk=pk)
    form = FeedKeywordSetsForm(request.POST or None, initial={"keyword_sets": feed.keyword_sets.all()})
    if request.method == "POST" and form.is_valid():
        chosen = set(form.cleaned_data["keyword_sets"])
        with transaction.atomic():
            FeedKeywordSet.objects.filter(feed=feed).exclude(keyword_set__in=chosen).delete()
            for kset in chosen:
                FeedKeywordSet.objects.get_or_create(feed=feed, keyword_set=kset)
        record_version(feed, comment="Keyword sets changed")
        audit("keyword_sets", feed, keyword_sets=[k.name for k in chosen])
        messages.success(request, "Keyword sets updated.")
        return redirect(feed.get_absolute_url() + "#keywords")
    return render_form(request, form, title=f"Keyword sets for {feed}", back_url=feed.get_absolute_url())


# --- Extraction rules ----------------------------------------------------------------
@admin_required
def rule_edit(request, feed_pk, pk=None):
    feed = get_object_or_404(Feed, pk=feed_pk)
    if pk:
        rule = get_object_or_404(ExtractionRule, pk=pk, feed=feed)
    else:
        rule_type = request.GET.get("type") or request.POST.get("rule_type")
        if not registry.has("extractor", rule_type or ""):
            return render(request, "ui/rules/choose_type.html", {"feed": feed, "plugins": registry.all("extractor")})
        last = feed.rules.order_by("-position").first()
        rule = ExtractionRule(feed=feed, rule_type=rule_type, position=(last.position + 10) if last else 10,
                              name=registry.get("extractor", rule_type).label)
    plugin, cform = _plugin_form(request, "extractor", rule.rule_type, rule.config, "c")
    form = RuleForm(request.POST or None, instance=rule)
    oform = _settings_form(request, rule.settings_override, effective_settings(feed), prefix="o")
    if request.method == "POST":
        if cform.is_valid() & oform.is_valid():
            rule.config = cform.config
            rule.settings_override = oform.config
            if form.is_valid():
                form.save()
                audit("update" if pk else "create", rule)
                messages.success(request, f"Saved rule {rule.name}.")
                return redirect(feed.get_absolute_url() + "#rules")
        messages.error(request, "Please fix the errors below.")
    return render_form(
        request, form, title=f"{'Edit' if pk else 'New'} rule — {plugin.label}", back_url=feed.get_absolute_url(),
        schema_forms=[(f"{plugin.label} options", cform), ("Settings override for this rule (optional)", oform)],
        method_help=plugin.description, hidden={"rule_type": rule.rule_type}, rule=rule if pk else None,
        collapse_last=True, test_panel={"feed": feed, "target": "rule", "object_pk": rule.pk or ""},
    )


@admin_required
def rule_delete(request, feed_pk, pk):
    rule = get_object_or_404(ExtractionRule, pk=pk, feed_id=feed_pk)
    url = rule.feed.get_absolute_url() + "#rules"
    return delete_with_confirm(request, rule, back_url=url, success_url=url)


@admin_required
@require_POST
def rule_action(request, feed_pk, pk, action):
    rule = get_object_or_404(ExtractionRule, pk=pk, feed_id=feed_pk)
    feed = rule.feed
    if action == "toggle":
        rule.enabled = not rule.enabled
        rule.save()
        audit("enable" if rule.enabled else "disable", rule)
    elif action == "clone":
        copy = services.clone_rule(rule)
        messages.success(request, f"Cloned as '{copy.name}' (disabled) — edit and enable it.")
    elif action in {"up", "down"}:
        ordered = list(feed.rules.all())
        i = ordered.index(rule)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(ordered):
            ordered[i], ordered[j] = ordered[j], ordered[i]
            services.reorder(feed.rules.all(), [r.pk for r in ordered])
    return redirect(feed.get_absolute_url() + "#rules")


# --- Custom fields -------------------------------------------------------------------
@admin_required
def field_edit(request, feed_pk, pk=None):
    feed = get_object_or_404(Feed, pk=feed_pk)
    if pk:
        field = get_object_or_404(CustomFieldDefinition, pk=pk, feed=feed)
    else:
        extractor = request.GET.get("extractor") or request.POST.get("extractor") or "regex"
        if not registry.has("field_extractor", extractor):
            extractor = "regex"
        last = feed.custom_fields.order_by("-position").first()
        field = CustomFieldDefinition(feed=feed, extractor=extractor, position=(last.position + 10) if last else 10)
    plugin, cform = _plugin_form(request, "field_extractor", field.extractor, field.config, "c")
    form = CustomFieldForm(request.POST or None, instance=field)
    if request.method == "POST":
        if cform.is_valid():
            field.config = cform.config
            if form.is_valid():
                form.save()
                audit("update" if pk else "create", field)
                messages.success(request, f"Saved custom field {field.name}.")
                return redirect(feed.get_absolute_url() + "#fields")
        messages.error(request, "Please fix the errors below.")
    return render_form(
        request, form, title=f"{'Edit' if pk else 'New'} custom field — {plugin.label}",
        back_url=feed.get_absolute_url(), schema_forms=[(f"{plugin.label} options", cform)],
        method_help=plugin.description, hidden={"extractor": field.extractor},
        extractor_choices=registry.choices("field_extractor") if not pk else None, current_extractor=field.extractor,
        test_panel={"feed": feed, "target": "field", "object_pk": field.pk or ""},
    )


@admin_required
def field_delete(request, feed_pk, pk):
    field = get_object_or_404(CustomFieldDefinition, pk=pk, feed_id=feed_pk)
    url = field.feed.get_absolute_url() + "#fields"
    return delete_with_confirm(request, field, back_url=url, success_url=url)


# --- Format profiles -----------------------------------------------------------------
@operator_required
def profile_list(request):
    profiles = FormatProfile.objects.annotate(feed_count=Count("feeds"))
    return render(request, "ui/profiles/list.html", {"profiles": profiles})


@operator_required
def profile_detail(request, pk):
    profile = get_object_or_404(FormatProfile, pk=pk)
    return render(request, "ui/profiles/detail.html", {
        "profile": profile, "feeds": profile.feeds.select_related("source"),
        "settings_rows": provenance_rows(profile.settings, {}), "versions": versions_for(profile)[:10],
        "sources": Source.objects.all(),
    })


@admin_required
def profile_edit(request, pk=None):
    profile = get_object_or_404(FormatProfile, pk=pk) if pk else FormatProfile()
    method = request.POST.get("method") or request.GET.get("method") or profile.method
    form = ProfileForm(request.POST or None, instance=profile, initial={"method": method})
    sform = _settings_form(request, profile.settings, effective_settings_defaults())
    plugin, mform = _plugin_form(request, "fetcher", method, profile.method_config if method == profile.method else {}, "m")
    schema_forms = []
    if mform is not None and mform.schema.get("properties"):
        schema_forms.append((f"{plugin.label} options", mform))
    schema_forms.append(("Default settings (blank = built-in default)", sform))
    if request.method == "POST":
        if sform.is_valid() & (mform is None or mform.is_valid()):
            profile.settings = sform.config
            profile.method_config = mform.config if mform is not None else {}
            if form.is_valid():
                form.save()
                audit("update" if pk else "create", profile)
                messages.success(request, f"Saved profile {profile}.")
                return redirect(profile.get_absolute_url())
        messages.error(request, "Please fix the errors below.")
    return render_form(request, form, title=f"Edit profile: {profile}" if pk else "New format profile",
                       back_url=profile.get_absolute_url() if pk else reverse("ui:profile_list"),
                       schema_forms=schema_forms)


@admin_required
def profile_delete(request, pk):
    profile = get_object_or_404(FormatProfile, pk=pk)
    return delete_with_confirm(request, profile, back_url=profile.get_absolute_url(),
                               success_url=reverse("ui:profile_list"),
                               warning="Feeds created from this profile keep their copied rules but lose its default settings.")


@admin_required
@require_POST
def profile_clone(request, pk):
    copy = services.clone_profile(get_object_or_404(FormatProfile, pk=pk))
    messages.success(request, f"Cloned as {copy}.")
    return redirect(reverse("ui:profile_edit", args=[copy.pk]))


@admin_required
@require_POST
def profile_create_feed(request, pk):
    profile = get_object_or_404(FormatProfile, pk=pk)
    source = get_object_or_404(Source, pk=request.POST.get("source"))
    name = request.POST.get("name") or profile.name
    feed = services.create_feed_from_profile(source, profile, name)
    messages.success(request, f"Created {feed} from {profile} (disabled).")
    return redirect(reverse("ui:feed_edit", args=[feed.pk]))


# --- Keyword sets --------------------------------------------------------------------
@operator_required
def keywordset_list(request):
    sets = KeywordSet.objects.annotate(keyword_count=Count("keywords", distinct=True),
                                       feed_count=Count("feeds", distinct=True))
    return render(request, "ui/keywords/list.html", {"sets": sets})


@operator_required
def keywordset_detail(request, pk):
    kset = get_object_or_404(KeywordSet, pk=pk)
    return render(request, "ui/keywords/detail.html", {
        "kset": kset, "keywords": kset.keywords.all(), "add_form": KeywordForm(),
        "import_form": KeywordCsvImportForm(), "versions": versions_for(kset)[:10], "feeds": kset.feeds.all(),
    })


@admin_required
def keywordset_edit(request, pk=None):
    kset = get_object_or_404(KeywordSet, pk=pk) if pk else KeywordSet()
    form = KeywordSetForm(request.POST or None, instance=kset)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit("update" if pk else "create", kset)
        return redirect(kset.get_absolute_url())
    return render_form(request, form, title=f"Edit {kset}" if pk else "New keyword set",
                       back_url=kset.get_absolute_url() if pk else reverse("ui:keywordset_list"))


@admin_required
def keywordset_delete(request, pk):
    kset = get_object_or_404(KeywordSet, pk=pk)
    return delete_with_confirm(request, kset, back_url=kset.get_absolute_url(),
                               success_url=reverse("ui:keywordset_list"))


@admin_required
@require_POST
def keywordset_clone(request, pk):
    kset = get_object_or_404(KeywordSet, pk=pk)
    with transaction.atomic(), version_context(change_type="clone", comment=f"Cloned from keyword set #{pk}"):
        copy = KeywordSet.objects.create(
            name=services.unique_value(KeywordSet, "name", f"{kset.name} (copy)", 200), description=kset.description,
        )
        Keyword.objects.bulk_create([Keyword(keyword_set=copy, **k.as_dict()) for k in kset.keywords.all()])
        record_version(copy, change_type="update", comment="Keywords copied")
    messages.success(request, f"Cloned as {copy}.")
    return redirect(copy.get_absolute_url())


@admin_required
def keyword_edit(request, set_pk, pk=None):
    kset = get_object_or_404(KeywordSet, pk=set_pk)
    keyword = get_object_or_404(Keyword, pk=pk, keyword_set=kset) if pk else Keyword(keyword_set=kset)
    form = KeywordForm(request.POST or None, instance=keyword)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            record_version(kset, comment=f"Keyword '{keyword.term}' {'edited' if pk else 'added'}")
            messages.success(request, f"Saved keyword {keyword.term}.")
            return redirect(kset.get_absolute_url())
        if not pk:
            messages.error(request, "Keyword not added: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
            return redirect(kset.get_absolute_url())
    return render_form(request, form, title=f"Edit keyword in {kset}", back_url=kset.get_absolute_url())


@admin_required
@require_POST
def keyword_delete(request, set_pk, pk):
    kset = get_object_or_404(KeywordSet, pk=set_pk)
    keyword = get_object_or_404(Keyword, pk=pk, keyword_set=kset)
    keyword.delete()
    record_version(kset, comment=f"Keyword '{keyword.term}' deleted")
    messages.success(request, f"Deleted keyword {keyword.term}.")
    return redirect(kset.get_absolute_url())


CSV_FIELDS = ["term", "language", "variants", "kind", "match_mode", "case_sensitive", "tag", "weight", "enabled"]


@operator_required
def keywordset_export_csv(request, pk):
    kset = get_object_or_404(KeywordSet, pk=pk)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_FIELDS)
    for k in kset.keywords.all():
        writer.writerow([k.term, k.language, "|".join(k.variants), k.kind, k.match_mode, k.case_sensitive,
                         k.tag, k.weight, k.enabled])
    # BOM so Excel opens Bangla correctly
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="keywords-{kset.pk}.csv"'
    return response


def parse_keyword_csv(text):
    """Return (rows as dicts, errors)."""
    rows, errors = [], []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "term" not in reader.fieldnames:
        return [], ["The CSV needs a header row with at least a 'term' column."]
    valid = {f: {c for c, _ in Keyword._meta.get_field(f).choices} for f in ("language", "kind", "match_mode")}
    for line_no, raw in enumerate(reader, start=2):
        term = (raw.get("term") or "").strip()
        if not term:
            continue
        row = {"term": term, "variants": [v.strip() for v in (raw.get("variants") or "").split("|") if v.strip()]}
        for field in ("language", "kind", "match_mode"):
            value = (raw.get(field) or "").strip()
            if value:
                if value not in valid[field]:
                    errors.append(f"Line {line_no}: {field} '{value}' must be one of {sorted(valid[field])}")
                    continue
                row[field] = value
        for field in ("case_sensitive", "enabled"):
            value = (raw.get(field) or "").strip().lower()
            if value:
                row[field] = value in {"1", "true", "yes", "y"}
        if (raw.get("tag") or "").strip():
            row["tag"] = raw["tag"].strip()
        if (raw.get("weight") or "").strip():
            try:
                row["weight"] = float(raw["weight"])
            except ValueError:
                errors.append(f"Line {line_no}: weight must be a number")
        rows.append(row)
    return rows, errors


@admin_required
@require_POST
def keywordset_import_csv(request, pk):
    kset = get_object_or_404(KeywordSet, pk=pk)
    form = KeywordCsvImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a CSV file.")
        return redirect(kset.get_absolute_url())
    try:
        text = form.cleaned_data["file"].read().decode("utf-8-sig")
    except UnicodeDecodeError:
        messages.error(request, "The file is not UTF-8. In Excel use 'Save As → CSV UTF-8'.")
        return redirect(kset.get_absolute_url())
    rows, errors = parse_keyword_csv(text)
    if errors:
        for error in errors[:20]:
            messages.error(request, error)
        return redirect(kset.get_absolute_url())
    with transaction.atomic():
        if form.cleaned_data["replace"]:
            kset.keywords.all().delete()
        existing = set(kset.keywords.values_list("term", flat=True))
        new = []
        for row in rows:
            keyword = Keyword(keyword_set=kset, **row)
            keyword.normalize()
            if keyword.term not in existing:
                existing.add(keyword.term)
                new.append(keyword)
        Keyword.objects.bulk_create(new)
        record_version(kset, comment=f"CSV import: {len(new)} keywords added")
    audit("import_keywords_csv", kset, added=len(new), skipped=len(rows) - len(new))
    messages.success(request, f"Imported {len(new)} keywords ({len(rows) - len(new)} already present).")
    return redirect(kset.get_absolute_url())
