from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class FetchRun(models.Model):
    TRIGGERS = [
        ("schedule", "Scheduled"), ("manual", "Run now"), ("test", "Test (dry run)"),
        ("upload", "Manual upload"), ("mailbox", "Mailbox poll"), ("reprocess", "Reprocess"),
    ]
    STATUSES = [
        ("running", "Running"), ("success", "Success"), ("partial", "Partial (some errors)"),
        ("failed", "Failed"), ("skipped", "Skipped"),
    ]
    feed = models.ForeignKey("sources.Feed", null=True, blank=True, on_delete=models.CASCADE, related_name="runs")
    mailbox = models.ForeignKey(
        "mailbox.MailboxAccount", null=True, blank=True, on_delete=models.CASCADE, related_name="runs"
    )
    trigger = models.CharField(max_length=10, choices=TRIGGERS, default="manual")
    dry_run = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUSES, default="running", db_index=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    stats = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    worker = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["feed", "-started_at"]), models.Index(fields=["mailbox", "-started_at"])]

    def __str__(self):
        target = self.feed or self.mailbox or "upload"
        return f"Run #{self.pk} {target} {self.status}"

    def get_absolute_url(self):
        return reverse("ui:run_detail", args=[self.pk])

    @property
    def duration(self):
        if self.finished_at:
            return self.finished_at - self.started_at
        return None


class WorkerHeartbeat(models.Model):
    """One row per running `manage.py run_worker` process, refreshed every minute."""

    name = models.CharField(max_length=150, unique=True)
    hostname = models.CharField(max_length=150)
    pid = models.PositiveIntegerField()
    started_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    running_feeds = models.JSONField(default=list, blank=True)
    info = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        return self.name

    @property
    def alive(self):
        from datetime import timedelta

        return self.last_seen_at >= timezone.now() - timedelta(minutes=3)


class FetchRunEvent(models.Model):
    LEVELS = [("debug", "Debug"), ("info", "Info"), ("warning", "Warning"), ("error", "Error")]
    run = models.ForeignKey(FetchRun, on_delete=models.CASCADE, related_name="events")
    created_at = models.DateTimeField(default=timezone.now)
    level = models.CharField(max_length=10, choices=LEVELS, default="info")
    message = models.TextField()
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["run", "level"])]

    def __str__(self):
        return f"[{self.level}] {self.message[:80]}"


class RawArtifact(models.Model):
    """Untouched original bytes, stored once on disk at a SHA-256 based path."""

    ORIGINS = [("http", "Downloaded"), ("email", "Email (.eml)"), ("attachment", "Email attachment"),
               ("upload", "Manual upload"), ("paste", "Pasted text"), ("archive", "From ZIP")]
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=200, blank=True)
    file_type = models.CharField(max_length=20, blank=True, db_index=True)
    original_filename = models.CharField(max_length=500, blank=True)
    storage_path = models.CharField(max_length=500)
    source_url = models.URLField(max_length=2000, blank=True)
    response_headers = models.JSONField(default=dict, blank=True)
    origin = models.CharField(max_length=12, choices=ORIGINS, default="http")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    first_feed = models.ForeignKey("sources.Feed", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    first_run = models.ForeignKey(FetchRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="artifacts")
    fetched_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    seen_count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-fetched_at"]

    def __str__(self):
        return f"{self.original_filename or self.source_url or self.sha256[:12]} ({self.file_type})"

    def get_absolute_url(self):
        return reverse("ui:artifact_download", args=[self.pk])

    @property
    def path(self):
        return settings.EWCE_ARTIFACT_ROOT / self.storage_path


class CollectedMessage(models.Model):
    STATUSES = [
        ("new", "New"), ("duplicate", "Duplicate"), ("updated", "Updated (new version)"),
        ("filtered_out", "Filtered out"), ("extraction_failed", "Extraction failed"),
        ("needs_attention", "Needs attention"), ("unrouted", "Unrouted email"),
    ]
    QUALITY_FLAGS = [
        "legacy_bangla_font", "ocr_used", "ocr_unavailable", "empty_body", "truncated", "low_confidence",
        "no_keyword_match", "js_not_rendered", "missing_required_field", "unparsed_file_type",
        "date_unparsed", "layout_changed",
    ]

    source = models.ForeignKey("sources.Source", null=True, blank=True, on_delete=models.PROTECT, related_name="messages")
    feed = models.ForeignKey("sources.Feed", null=True, blank=True, on_delete=models.PROTECT, related_name="messages")
    mailbox = models.ForeignKey(
        "mailbox.MailboxAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    raw_artifact = models.ForeignKey(RawArtifact, null=True, blank=True, on_delete=models.PROTECT, related_name="messages")
    fetch_run = models.ForeignKey(FetchRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")

    source_url = models.URLField(max_length=2000, blank=True)
    source_item_key = models.CharField(max_length=512, blank=True)
    title = models.CharField(max_length=1000, blank=True)
    body_text = models.TextField(blank=True)  # LONGTEXT on MySQL/MariaDB; NFC-normalized
    body_html = models.TextField(blank=True)
    raw_extracted_text = models.TextField(
        blank=True, help_text="Text as extracted when it cannot be trusted as clean text (e.g. legacy-font Bangla)"
    )
    search_text = models.TextField(blank=True, help_text="NFC + ASCII digits + casefold, for search")
    detected_language = models.CharField(max_length=10, blank=True, db_index=True)
    published_at_text = models.CharField(max_length=300, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    collected_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    seen_count = models.PositiveIntegerField(default=1)
    content_hash = models.CharField(max_length=64, db_index=True)

    version = models.PositiveIntegerField(default=1)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by"
    )
    is_latest = models.BooleanField(default=True, db_index=True)
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates"
    )

    status = models.CharField(max_length=20, choices=STATUSES, default="new", db_index=True)
    attention_reason = models.TextField(blank=True)
    matched_keywords = models.JSONField(default=list, blank=True)
    keyword_score = models.FloatField(default=0)
    extra_fields = models.JSONField(default=dict, blank=True)
    extraction_method = models.CharField(max_length=100, blank=True)
    extracting_rule = models.ForeignKey(
        "sources.ExtractionRule", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    extraction_confidence = models.FloatField(null=True, blank=True)
    quality_flags = models.JSONField(default=list, blank=True)

    manual_entry = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Reserved for Scope 2 (sanitizing/classification/approval). Not used in Scope 1.
    processing_state = models.CharField(max_length=30, default="pending", db_index=True)
    processing_meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-collected_at", "-id"]
        indexes = [
            models.Index(fields=["feed", "source_item_key"], name="msg_feed_key_idx"),
            models.Index(fields=["source", "-collected_at"], name="msg_source_time_idx"),
            models.Index(fields=["status", "-collected_at"], name="msg_status_time_idx"),
            models.Index(fields=["feed", "content_hash"], name="msg_feed_hash_idx"),
        ]

    def __str__(self):
        return self.title[:80] or f"Message #{self.pk}"

    def get_absolute_url(self):
        return reverse("ui:message_detail", args=[self.pk])

    def version_chain(self):
        """All versions of this item, newest first."""
        if not self.source_item_key or not self.feed_id:
            return CollectedMessage.objects.filter(pk=self.pk)
        return CollectedMessage.objects.filter(
            feed_id=self.feed_id, source_item_key=self.source_item_key
        ).exclude(status="duplicate").order_by("-version", "-id")
