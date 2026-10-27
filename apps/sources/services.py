"""Config services: create a feed from a profile, clone feeds/profiles/rules, save profile from feed."""
from django.db import transaction
from django.utils.text import slugify

from apps.core.audit import audit
from apps.core.versioning import version_context

from .models import CustomFieldDefinition, ExtractionRule, Feed, FeedKeywordSet, FormatProfile


def unique_value(model, field, base, max_length, **scope):
    candidate = base[:max_length]
    n = 2
    while model.objects.filter(**{field: candidate}, **scope).exists():
        suffix = f"-{n}" if field in {"slug", "code"} else f" ({n})"
        candidate = base[: max_length - len(suffix)] + suffix
        n += 1
    return candidate


def apply_profile_templates(feed, profile, replace=False):
    """Copy the profile's rules and custom fields into the feed."""
    if replace:
        feed.rules.all().delete()
        feed.custom_fields.all().delete()
    offset = feed.rules.count()
    for i, tpl in enumerate(profile.rules_template or [], start=1):
        rule = ExtractionRule(
            feed=feed,
            position=(offset + i) * 10,
            name=tpl.get("name") or tpl["rule_type"],
            enabled=tpl.get("enabled", True),
            rule_type=tpl["rule_type"],
            applies_to_types=tpl.get("applies_to_types", []),
            applies_to_url_pattern=tpl.get("applies_to_url_pattern", ""),
            config=tpl.get("config", {}),
            settings_override=tpl.get("settings_override", {}),
            on_no_match=tpl.get("on_no_match", "continue"),
            notes=tpl.get("notes", ""),
        )
        rule.full_clean()
        rule.save()
    existing = set(feed.custom_fields.values_list("name", flat=True))
    for i, tpl in enumerate(profile.custom_fields_template or [], start=1):
        if tpl["name"] in existing:
            continue
        field = CustomFieldDefinition(
            feed=feed,
            position=i * 10,
            name=tpl["name"],
            label=tpl.get("label", ""),
            extractor=tpl["extractor"],
            config=tpl.get("config", {}),
            enabled=tpl.get("enabled", True),
            applies_to_types=tpl.get("applies_to_types", []),
            required=tpl.get("required", False),
        )
        field.full_clean()
        field.save()


@transaction.atomic
def create_feed_from_profile(source, profile, name, **fields):
    feed = Feed(
        source=source,
        profile=profile,
        name=name,
        slug=unique_value(Feed, "slug", slugify(name) or "feed", 100, source=source),
        method=profile.method,
        method_config=dict(profile.method_config or {}),
        **fields,
    )
    feed.full_clean()
    feed.save()
    apply_profile_templates(feed, profile)
    audit("create_from_profile", feed, profile=profile.name)
    return feed


@transaction.atomic
def clone_feed(feed, *, name=None, source=None):
    source = source or feed.source
    name = name or unique_value(Feed, "name", f"{feed.name} (copy)", 200, source=source)
    rules = list(feed.rules.all())
    fields = list(feed.custom_fields.all())
    kw_links = list(FeedKeywordSet.objects.filter(feed=feed))
    original_pk = feed.pk
    with version_context(change_type="clone", comment=f"Cloned from feed #{original_pk}"):
        copy = Feed.objects.get(pk=original_pk)
        copy.pk = None
        copy._state.adding = True
        copy.source = source
        copy.name = name
        copy.slug = unique_value(Feed, "slug", slugify(name) or "feed", 100, source=source)
        copy.is_active = False
        for attr in Feed.version_exclude_fields:
            setattr(copy, attr, Feed._meta.get_field(attr).get_default())
        copy.save()
        for rule in rules:
            rule.pk = None
            rule._state.adding = True
            rule.feed = copy
            rule.save()
        for field in fields:
            field.pk = None
            field._state.adding = True
            field.feed = copy
            field.save()
        for link in kw_links:
            FeedKeywordSet.objects.create(feed=copy, keyword_set_id=link.keyword_set_id, enabled=link.enabled)
    audit("clone", copy, cloned_from=original_pk)
    return copy


@transaction.atomic
def clone_profile(profile, name=None):
    name = name or unique_value(FormatProfile, "name", f"{profile.name} (copy)", 200)
    original_pk = profile.pk
    with version_context(change_type="clone", comment=f"Cloned from profile #{original_pk}"):
        copy = FormatProfile.objects.get(pk=original_pk)
        copy.pk = None
        copy._state.adding = True
        copy.name = name
        copy.slug = unique_value(FormatProfile, "slug", slugify(name) or "profile", 100)
        copy.is_example = False
        copy.save()
    audit("clone", copy, cloned_from=original_pk)
    return copy


@transaction.atomic
def clone_rule(rule, feed=None):
    original_pk = rule.pk
    with version_context(change_type="clone", comment=f"Cloned from rule #{original_pk}"):
        copy = ExtractionRule.objects.get(pk=original_pk)
        copy.pk = None
        copy._state.adding = True
        copy.feed = feed or rule.feed
        copy.name = f"{rule.name} (copy)"[:200]
        copy.enabled = False
        copy.position = rule.position + 1
        copy.save()
    return copy


@transaction.atomic
def save_feed_as_profile(feed, name):
    profile = FormatProfile(
        name=unique_value(FormatProfile, "name", name, 200),
        slug=unique_value(FormatProfile, "slug", slugify(name) or "profile", 100),
        description=f"Created from feed {feed}",
        method=feed.method,
        method_config=feed.method_config,
        settings=feed.settings,
        rules_template=[r.as_template() for r in feed.rules.all()],
        custom_fields_template=[f.as_template() for f in feed.custom_fields.all()],
    )
    profile.full_clean()
    profile.save()
    audit("save_as_profile", profile, feed=feed.pk)
    return profile


def reorder(queryset, ordered_ids):
    """Set position = 10, 20, 30... following ordered_ids, and record a version for each moved object."""
    by_id = {obj.pk: obj for obj in queryset}
    with version_context(comment="Reordered"):
        for index, pk in enumerate(ordered_ids, start=1):
            obj = by_id.get(int(pk))
            if obj is not None and obj.position != index * 10:
                obj.position = index * 10
                obj.save(update_fields=["position", "updated_at"])
