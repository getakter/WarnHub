from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from .middleware import get_current_user


class TrackedModel(models.Model):
    """created/updated timestamps and users for every configuration table."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        user = get_current_user()
        update_fields = kwargs.get("update_fields")
        if user is not None:
            if self._state.adding and self.created_by_id is None:
                self.created_by = user
            if update_fields is None:
                self.updated_by = user
        super().save(*args, **kwargs)


class VersionedMixin:
    """Config models that are snapshotted to ConfigVersion on save (see core.versioning).

    ``version_exclude_fields`` lists runtime/state fields (health, locks, timestamps)
    that are neither snapshotted nor restored on rollback.
    """

    version_exclude_fields = ()

    def snapshot(self):
        from .versioning import model_snapshot

        return model_snapshot(self)

    def apply_snapshot(self, data):
        from .versioning import apply_model_snapshot

        apply_model_snapshot(self, data)


class ConfigVersion(models.Model):
    CHANGE_TYPES = [
        ("create", "Created"),
        ("update", "Updated"),
        ("rollback", "Rolled back"),
        ("import", "Imported"),
        ("clone", "Cloned"),
        ("delete", "Deleted"),
    ]
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    object_repr = models.CharField(max_length=300)
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    change_type = models.CharField(max_length=20, choices=CHANGE_TYPES, default="update")
    comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id", "version"], name="uniq_config_version"
            )
        ]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.object_repr} v{self.version}"


class AuditLog(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=50, db_index=True)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=300, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_repr}"
