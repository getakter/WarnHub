import re

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from apps.core.models import TrackedModel, VersionedMixin

LANGUAGE_CHOICES = [("auto", "Auto-detect"), ("bn", "Bangla"), ("en", "English"), ("mixed", "Mixed")]
ENTRY_URL_PLACEHOLDERS = re.compile(r"\{(YYYY|YY|MM|M|DD|D)\}")


def _plugin_errors(kind, name, config, label):
    from apps.plugins.registry import PluginNotFound, registry

    try:
        plugin = registry.get(kind, name)
    except PluginNotFound as exc:
        return [str(exc)]
    return [f"{label}: {issue}" for issue in plugin.validate_config(config or {})]


def _settings_errors(data, label):
    from apps.plugins.schema_forms import _strip_required
    from apps.plugins.validation import validate

    from .settings_schema import SETTINGS_SCHEMA

    return [f"{label}: {issue}" for issue in validate(_strip_required(SETTINGS_SCHEMA), data or {})]


class Source(VersionedMixin, TrackedModel):
    """An organization that publishes information (e.g. a met department)."""

    name = models.CharField(max_length=200, unique=True)
    code = models.SlugField(max_length=40, unique=True, help_text="Short code, e.g. BMD. Used in exports.")
    base_url = models.URLField(max_length=500, blank=True, verbose_name="Website / base URL")
    description = models.TextField(blank=True)
    default_language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default="auto")
    default_timezone = models.CharField(max_length=64, default="Asia/Dhaka")
    default_user_agent = models.CharField(max_length=300, blank=True, verbose_name="Default User-Agent")
    default_headers = models.JSONField(default=dict, blank=True)
    contact_person = models.CharField(max_length=300, blank=True)
    permission_notes = models.TextField(
        blank=True, help_text="e.g. 'Written approval received on 2026-01-15 from ...' or terms of use."
    )
    logo = models.FileField(upload_to="logos/", blank=True)
    color = models.CharField(max_length=7, default="#0d6efd", help_text="Colour tag, e.g. #0d6efd")
    settings = models.JSONField(default=dict, blank=True, help_text="Default settings for all feeds of this source.")
    is_active = models.BooleanField(default=True)
    is_example = models.BooleanField(default=False, help_text="Seeded example data (safe to edit or delete).")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def get_absolute_url(self):
        return reverse("ui:source_detail", args=[self.pk])

    def clean(self):
        errors = _settings_errors(self.settings, "Settings")
        if self.color and not re.fullmatch(r"#[0-9a-fA-F]{6}", self.color):
            errors.append("Colour must look like #1a2b3c")
        if not isinstance(self.default_headers, dict):
            errors.append("Default headers must be a JSON object")
        if errors:
            raise ValidationError(errors)

    def settings_layer(self):
        """Source fields expressed as a settings layer, then explicit source settings on top."""
        from apps.core.merge import deep_merge

        derived = {"content": {}, "fetch": {}}
        if self.default_language and self.default_language != "auto":
            derived["content"]["language"] = self.default_language
        if self.default_timezone:
            derived["content"]["timezone"] = self.default_timezone
        if self.default_user_agent:
            derived["fetch"]["user_agent"] = self.default_user_agent
        if self.default_headers:
            derived["fetch"]["headers"] = dict(self.default_headers)
        return deep_merge(derived, self.settings or {})


class FormatProfile(VersionedMixin, TrackedModel):
    """Reusable template: default method, settings, extraction rules and custom fields."""

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    method = models.CharField(max_length=60, default="web_page", help_text="Default acquisition method")
    method_config = models.JSONField(default=dict, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    rules_template = models.JSONField(default=list, blank=True, help_text="Rules copied into new feeds")
    custom_fields_template = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    is_example = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("ui:profile_detail", args=[self.pk])

    def clean(self):
        if not self.slug:
            self.slug = slugify(self.name)[:100]
        errors = _settings_errors(self.settings, "Settings")
        errors += _plugin_errors("fetcher", self.method, self.method_config, "Method options")
        for i, rule in enumerate(self.rules_template or [], start=1):
            if not isinstance(rule, dict) or "rule_type" not in rule:
                errors.append(f"Rule template #{i} must be an object with a rule_type")
                continue
            errors += _plugin_errors("extractor", rule["rule_type"], rule.get("config"), f"Rule #{i} ({rule.get('name', '')})")
        for i, field in enumerate(self.custom_fields_template or [], start=1):
            if not isinstance(field, dict) or "name" not in field or "extractor" not in field:
                errors.append(f"Custom field template #{i} needs name and extractor")
                continue
            errors += _plugin_errors("field_extractor", field["extractor"], field.get("config"), f"Custom field {field['name']}")
        if errors:
            raise ValidationError(errors)


class Feed(VersionedMixin, TrackedModel):
    """A section of a source collected one way (a page, a listing, an RSS feed, a mailbox route...)."""

    SCHEDULE_CHOICES = [("manual", "Manual only"), ("interval", "Every N minutes"), ("cron", "Cron expression")]
    HEALTH_CHOICES = [
        ("unknown", "Not run yet"), ("healthy", "Healthy"), ("degraded", "Degraded"),
        ("silent", "Silent"), ("failing", "Failing"), ("disabled", "Disabled"),
    ]

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="feeds")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True, help_text="Notes for admins, e.g. 'configure me: ...'")
    is_active = models.BooleanField(default=False)
    tags = models.JSONField(default=list, blank=True)
    method = models.CharField(max_length=60, default="web_page", verbose_name="Acquisition method")
    method_config = models.JSONField(default=dict, blank=True)
    profile = models.ForeignKey(FormatProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="feeds")
    entry_urls = models.JSONField(
        default=list, blank=True,
        help_text="One URL per line. Placeholders: {YYYY} {YY} {MM} {M} {DD} {D} (today, source timezone).",
    )
    settings = models.JSONField(default=dict, blank=True, help_text="Overrides on top of source and profile")

    schedule_type = models.CharField(max_length=10, choices=SCHEDULE_CHOICES, default="manual")
    interval_minutes = models.PositiveIntegerField(default=60)
    cron_expression = models.CharField(max_length=100, blank=True, help_text="e.g. 0 */3 * * * (min hour day month dow)")
    active_hours_start = models.TimeField(null=True, blank=True, help_text="Only run between these times (local)")
    active_hours_end = models.TimeField(null=True, blank=True)
    expected_frequency_hours = models.FloatField(
        null=True, blank=True, help_text="Flag the feed 'silent' if no new message for this long"
    )
    max_items_per_run = models.PositiveIntegerField(default=50)
    lookback_days = models.PositiveIntegerField(
        default=7, help_text="Ignore items with a published date older than this (0 = no limit)"
    )
    backfill_on_first_run = models.BooleanField(
        default=False, help_text="On the very first run ignore the lookback window and collect everything listed"
    )
    keyword_sets = models.ManyToManyField("KeywordSet", through="FeedKeywordSet", blank=True, related_name="feeds")

    # Runtime state (not versioned)
    health_status = models.CharField(max_length=10, choices=HEALTH_CHOICES, default="unknown", db_index=True)
    health_detail = models.TextField(blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_new_message_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    run_requested_at = models.DateTimeField(null=True, blank=True, help_text="Set by 'Run in background'")
    lock_owner = models.CharField(max_length=100, blank=True)
    lock_expires_at = models.DateTimeField(null=True, blank=True)
    first_run_done = models.BooleanField(default=False)
    last_extraction_stats = models.JSONField(default=dict, blank=True)

    version_exclude_fields = (
        "health_status", "health_detail", "last_run_at", "last_success_at", "last_new_message_at",
        "last_error_at", "last_error", "consecutive_failures", "next_run_at", "lock_owner",
        "lock_expires_at", "first_run_done", "last_extraction_stats", "run_requested_at",
    )
    SCHEDULE_FIELDS = ("is_active", "schedule_type", "interval_minutes", "cron_expression")

    class Meta:
        ordering = ["source__name", "name"]
        constraints = [models.UniqueConstraint(fields=["source", "slug"], name="uniq_feed_slug_per_source")]

    def __str__(self):
        return f"{self.source.code} / {self.name}"

    def get_absolute_url(self):
        return reverse("ui:feed_detail", args=[self.pk])

    def clean(self):
        errors = []
        if not self.slug and self.name:
            self.slug = slugify(self.name)[:100]
        errors += _settings_errors(self.settings, "Settings")
        errors += _plugin_errors("fetcher", self.method, self.method_config, "Method options")
        if not isinstance(self.entry_urls, list):
            errors.append("Entry URLs must be a list")
        else:
            for url in self.entry_urls:
                probe = ENTRY_URL_PLACEHOLDERS.sub("1", str(url))
                if not re.match(r"^https?://[^\s/$.?#].[^\s]*$", probe, re.IGNORECASE):
                    errors.append(f"Entry URL {url!r} is not a valid http(s) URL")
        if self.schedule_type == "cron":
            from apps.collection.scheduling import validate_cron

            message = validate_cron(self.cron_expression)
            if message:
                errors.append(f"Cron expression: {message}")
        if self.schedule_type == "interval" and self.interval_minutes < 1:
            errors.append("Interval must be at least 1 minute")
        if self.is_active and self.method_requires_urls() and not self.entry_urls:
            errors.append("An active feed using this method needs at least one entry URL")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if kwargs.get("update_fields") is None:
            self._reschedule()
        super().save(*args, **kwargs)

    def _reschedule(self):
        """Keep next_run_at in step with the schedule: activation -> run soon; schedule change -> recompute."""
        from django.utils import timezone

        from apps.collection.scheduling import next_run_for_feed

        if not self.is_active or self.schedule_type == "manual":
            self.next_run_at = None
            return
        previous = Feed.objects.filter(pk=self.pk).values(*self.SCHEDULE_FIELDS).first() if self.pk else None
        if previous is None or not previous["is_active"] or previous["schedule_type"] == "manual" or self.next_run_at is None:
            self.next_run_at = timezone.now()
        elif any(previous[f] != getattr(self, f) for f in self.SCHEDULE_FIELDS):
            self.next_run_at = next_run_for_feed(self)

    def method_requires_urls(self):
        from apps.plugins.registry import PluginNotFound, registry

        try:
            return getattr(registry.get("fetcher", self.method), "requires_entry_urls", True)
        except PluginNotFound:
            return False

    @property
    def is_locked(self):
        from django.utils import timezone

        return bool(self.lock_owner and self.lock_expires_at and self.lock_expires_at > timezone.now())


class ExtractionRule(VersionedMixin, TrackedModel):
    ON_NO_MATCH = [
        ("continue", "Continue with the next rule"),
        ("flag", "Continue, but flag needs_attention"),
        ("fail", "Stop: mark extraction_failed"),
    ]
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0, db_index=True)
    rule_type = models.CharField(max_length=60)
    applies_to_types = models.JSONField(default=list, blank=True, help_text="File types; empty = all")
    applies_to_url_pattern = models.CharField(
        max_length=500, blank=True, help_text="Wildcard (or re: prefix for regex); empty = all URLs"
    )
    config = models.JSONField(default=dict, blank=True)
    settings_override = models.JSONField(default=dict, blank=True)
    on_no_match = models.CharField(max_length=10, choices=ON_NO_MATCH, default="continue")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["feed", "position", "id"]

    def __str__(self):
        return f"{self.feed} › #{self.position} {self.name}"

    def get_absolute_url(self):
        return reverse("ui:rule_edit", args=[self.feed_id, self.pk])

    def clean(self):
        errors = _plugin_errors("extractor", self.rule_type, self.config, "Rule options")
        errors += _settings_errors(self.settings_override, "Settings override")
        if self.applies_to_url_pattern.startswith("re:"):
            try:
                re.compile(self.applies_to_url_pattern[3:])
            except re.error as exc:
                errors.append(f"URL pattern regex: {exc}")
        if errors:
            raise ValidationError(errors)

    def as_template(self):
        return {
            "name": self.name, "enabled": self.enabled, "rule_type": self.rule_type,
            "applies_to_types": self.applies_to_types, "applies_to_url_pattern": self.applies_to_url_pattern,
            "config": self.config, "settings_override": self.settings_override,
            "on_no_match": self.on_no_match, "notes": self.notes,
        }


class CustomFieldDefinition(VersionedMixin, TrackedModel):
    """Admin-defined extra field (e.g. bulletin_number) captured into CollectedMessage.extra_fields."""

    feed = models.ForeignKey(Feed, on_delete=models.CASCADE, related_name="custom_fields")
    name = models.SlugField(max_length=60, help_text="Key in extra_fields, e.g. bulletin_number")
    label = models.CharField(max_length=200, blank=True)
    extractor = models.CharField(max_length=60, default="regex")
    config = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)
    applies_to_types = models.JSONField(default=list, blank=True)
    required = models.BooleanField(default=False, help_text="Flag needs_attention when no value is found")

    class Meta:
        ordering = ["feed", "position", "id"]
        constraints = [models.UniqueConstraint(fields=["feed", "name"], name="uniq_custom_field_per_feed")]

    def __str__(self):
        return f"{self.feed} › {self.name}"

    def get_absolute_url(self):
        return reverse("ui:field_edit", args=[self.feed_id, self.pk])

    def clean(self):
        errors = _plugin_errors("field_extractor", self.extractor, self.config, "Field options")
        if self.name in {"title", "body_text", "published_at", "source_url", "source_item_key"}:
            errors.append(f"'{self.name}' is a core field; pick another name")
        if errors:
            raise ValidationError(errors)

    def as_template(self):
        return {
            "name": self.name, "label": self.label, "extractor": self.extractor, "config": self.config,
            "enabled": self.enabled, "applies_to_types": self.applies_to_types, "required": self.required,
        }


class KeywordSet(VersionedMixin, TrackedModel):
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_example = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("ui:keywordset_detail", args=[self.pk])

    def snapshot(self):
        data = super().snapshot()
        data["keywords"] = [k.as_dict() for k in self.keywords.all()] if self.pk else []
        return data

    def apply_snapshot(self, data):
        super().apply_snapshot(data)
        self._pending_keywords = data.get("keywords")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        pending = getattr(self, "_pending_keywords", None)
        if pending is not None:
            self.keywords.all().delete()
            rows = [Keyword(keyword_set=self, **Keyword.clean_dict(k)) for k in pending]
            for row in rows:
                row.normalize()
            Keyword.objects.bulk_create(rows)
            self._pending_keywords = None


class Keyword(models.Model):
    KIND_CHOICES = [("include", "Include"), ("exclude", "Exclude"), ("required", "Required")]
    MATCH_CHOICES = [
        ("whole_word", "Whole word"), ("prefix", "Word prefix (Bangla suffixes)"),
        ("substring", "Anywhere"), ("regex", "Regular expression"),
    ]
    LANG_CHOICES = [("any", "Any"), ("bn", "Bangla"), ("en", "English")]
    keyword_set = models.ForeignKey(KeywordSet, on_delete=models.CASCADE, related_name="keywords")
    term = models.CharField(max_length=300)
    language = models.CharField(max_length=5, choices=LANG_CHOICES, default="any")
    variants = models.JSONField(default=list, blank=True, help_text="Synonyms / spelling variants")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="include")
    match_mode = models.CharField(max_length=12, choices=MATCH_CHOICES, default="whole_word")
    case_sensitive = models.BooleanField(default=False)
    tag = models.CharField(max_length=100, blank=True, help_text="Optional tag/category, e.g. flood")
    weight = models.FloatField(default=1.0)
    enabled = models.BooleanField(default=True)

    FIELDS = ["term", "language", "variants", "kind", "match_mode", "case_sensitive", "tag", "weight", "enabled"]

    class Meta:
        ordering = ["keyword_set", "kind", "term"]

    def __str__(self):
        return self.term

    def save(self, *args, **kwargs):
        self.normalize()
        super().save(*args, **kwargs)

    def normalize(self):
        """NFC the term and variants (Bangla can be typed in several code-point orders) and dedupe."""
        from apps.core.bangla import nfc

        self.term = nfc(self.term).strip()
        seen, variants = {self.term}, []
        for variant in self.variants or []:
            variant = nfc(str(variant)).strip()
            if variant and variant not in seen:
                seen.add(variant)
                variants.append(variant)
        self.variants = variants

    def clean(self):
        if self.match_mode == "regex":
            for pattern in [self.term, *(self.variants or [])]:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValidationError(f"Invalid regex {pattern!r}: {exc}") from exc

    def as_dict(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def clean_dict(cls, data):
        return {f: data[f] for f in cls.FIELDS if f in data}


class FeedKeywordSet(models.Model):
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE)
    keyword_set = models.ForeignKey(KeywordSet, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["feed", "keyword_set"], name="uniq_feed_keywordset")]

    def __str__(self):
        return f"{self.feed} ↔ {self.keyword_set}"
