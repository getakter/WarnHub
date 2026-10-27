from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TrackedModel, VersionedMixin
from apps.sources.settings_schema import FILE_TYPE_LABELS, FILE_TYPES

POST_ACTIONS = [
    ("none", "Leave untouched"), ("mark_read", "Mark as read"),
    ("move", "Move to folder"), ("label", "Add label / flag"),
]


class MailboxAccount(VersionedMixin, TrackedModel):
    SECURITY = [("ssl", "SSL/TLS (port 993)"), ("starttls", "STARTTLS (port 143)"), ("none", "None (not recommended)")]

    label = models.CharField(max_length=200, unique=True)
    protocol = models.CharField(max_length=40, default="imap", help_text="Mailbox protocol plugin")
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=993)
    security = models.CharField(max_length=10, choices=SECURITY, default="ssl")
    username = models.CharField(max_length=255)
    password_env_var = models.CharField(
        max_length=100, help_text="NAME of the .env variable holding the password / app password (never the value)"
    )
    folders = models.JSONField(default=list, blank=True, help_text="Folders to watch, one per line (default INBOX)")
    poll_interval_minutes = models.PositiveIntegerField(default=10)
    max_messages_per_run = models.PositiveIntegerField(default=50)
    initial_lookback_days = models.PositiveIntegerField(default=7, help_text="On first poll, only look this far back")
    only_unseen = models.BooleanField(default=False, help_text="Only fetch messages not yet marked read on the server")
    default_post_action = models.CharField(max_length=10, choices=POST_ACTIONS, default="none")
    default_post_target = models.CharField(max_length=200, blank=True, help_text="Folder or label for move/label")
    unmatched_post_action = models.CharField(max_length=10, choices=POST_ACTIONS, default="none")
    options = models.JSONField(default=dict, blank=True, help_text="Protocol plugin options")
    is_active = models.BooleanField(default=False)
    is_example = models.BooleanField(default=False)

    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True)
    lock_owner = models.CharField(max_length=100, blank=True)
    lock_expires_at = models.DateTimeField(null=True, blank=True)

    version_exclude_fields = (
        "last_polled_at", "last_success_at", "last_error", "next_poll_at", "lock_owner", "lock_expires_at",
    )

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return self.label

    def get_absolute_url(self):
        return reverse("ui:mailbox_detail", args=[self.pk])

    def clean(self):
        import re

        errors = []
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.password_env_var or ""):
            errors.append("Password env var must be a variable NAME like EWCE_MAILBOX_ALERTS_PASSWORD, not the password.")
        if not isinstance(self.folders, list):
            errors.append("Folders must be a list")
        from apps.plugins.registry import PluginNotFound, registry

        try:
            plugin = registry.get("mailbox_protocol", self.protocol)
            errors += [f"Options: {i}" for i in plugin.validate_config(self.options or {})]
        except PluginNotFound as exc:
            errors.append(str(exc))
        if errors:
            raise ValidationError(errors)

    def watched_folders(self):
        return self.folders or ["INBOX"]


class MailboxRule(VersionedMixin, TrackedModel):
    TAKE = [("body", "Body only"), ("attachments", "Attachments only"), ("both", "Body and attachments")]
    SPLIT = [("per_email", "One message per email"), ("per_attachment", "One message per attachment (+ body)")]
    MATCH_MODE = [("all", "All conditions must match"), ("any", "Any condition matches")]
    HAS_ATTACHMENT = [("any", "Doesn't matter"), ("yes", "Must have attachments"), ("no", "Must have none")]

    mailbox = models.ForeignKey(MailboxAccount, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)
    match_mode = models.CharField(max_length=3, choices=MATCH_MODE, default="all")
    from_patterns = models.JSONField(default=list, blank=True, help_text="Addresses or @domain, one per line")
    to_patterns = models.JSONField(default=list, blank=True, help_text="Recipient addresses or @domain")
    subject_contains = models.JSONField(default=list, blank=True, help_text="Any of these phrases (Bangla ok)")
    subject_regex = models.CharField(max_length=500, blank=True)
    body_keywords = models.JSONField(default=list, blank=True)
    has_attachment = models.CharField(max_length=3, choices=HAS_ATTACHMENT, default="any")
    attachment_types = models.JSONField(default=list, blank=True, help_text="Match only if an attachment has one of these types")

    target_feed = models.ForeignKey("sources.Feed", on_delete=models.PROTECT, related_name="mailbox_rules")
    take = models.CharField(max_length=12, choices=TAKE, default="both")
    allowed_attachment_types = models.JSONField(default=list, blank=True, help_text="Empty = feed's allowed types")
    max_attachment_mb = models.FloatField(default=25)
    ignore_inline_images = models.BooleanField(default=True)
    split = models.CharField(max_length=15, choices=SPLIT, default="per_attachment")
    post_action = models.CharField(
        max_length=10, choices=[("default", "Mailbox default")] + POST_ACTIONS, default="default"
    )
    post_target = models.CharField(max_length=200, blank=True)
    stop_processing = models.BooleanField(default=True, help_text="Don't evaluate later rules after a match")

    class Meta:
        ordering = ["mailbox", "position", "id"]

    def __str__(self):
        return f"{self.mailbox} › #{self.position} {self.name}"

    def get_absolute_url(self):
        return reverse("ui:mailbox_rule_edit", args=[self.mailbox_id, self.pk])

    def clean(self):
        import re

        errors = []
        if self.subject_regex:
            try:
                re.compile(self.subject_regex)
            except re.error as exc:
                errors.append(f"Subject regex: {exc}")
        for field in ("attachment_types", "allowed_attachment_types"):
            bad = [t for t in getattr(self, field) or [] if t not in FILE_TYPES]
            if bad:
                errors.append(f"{field}: unknown type(s) {bad}; allowed: {', '.join(FILE_TYPES)}")
        if self.post_action in {"move", "label"} and not self.post_target:
            errors.append("Move/label needs a target folder or label")
        if errors:
            raise ValidationError(errors)

    FILE_TYPE_CHOICES = [(t, FILE_TYPE_LABELS[t]) for t in FILE_TYPES]


class MailboxSeenMessage(models.Model):
    """Prevents reprocessing: one row per email processed (by Message-ID and folder UID)."""

    OUTCOMES = [("routed", "Routed"), ("unrouted", "Unrouted"), ("error", "Error"), ("skipped", "Skipped")]
    mailbox = models.ForeignKey(MailboxAccount, on_delete=models.CASCADE, related_name="seen")
    folder = models.CharField(max_length=200)
    uid = models.CharField(max_length=50)
    uidvalidity = models.CharField(max_length=50, blank=True)
    message_id = models.CharField(max_length=500, blank=True, db_index=True)
    subject = models.CharField(max_length=1000, blank=True)
    from_address = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(default=timezone.now)
    rule = models.ForeignKey(MailboxRule, null=True, blank=True, on_delete=models.SET_NULL)
    raw_artifact = models.ForeignKey("collection.RawArtifact", null=True, blank=True, on_delete=models.SET_NULL)
    outcome = models.CharField(max_length=10, choices=OUTCOMES, default="routed")
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ["-processed_at"]
        constraints = [
            models.UniqueConstraint(fields=["mailbox", "folder", "uidvalidity", "uid"], name="uniq_mail_uid"),
        ]

    def __str__(self):
        return f"{self.mailbox} {self.folder}#{self.uid} {self.subject[:50]}"
