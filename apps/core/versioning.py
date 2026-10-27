"""Automatic JSON snapshots of configuration objects, with rollback."""
import contextvars
import json
from contextlib import contextmanager

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.db.models.signals import post_delete, post_save

from .middleware import get_current_user

TRACKING_FIELDS = {"id", "created_at", "updated_at", "created_by", "updated_by"}

_ctx = contextvars.ContextVar("ewce_version_ctx", default=None)


@contextmanager
def version_context(change_type="update", comment="", suppress=False):
    """Annotate (or suppress) versions recorded inside the block."""
    token = _ctx.set({"change_type": change_type, "comment": comment, "suppress": suppress})
    try:
        yield
    finally:
        _ctx.reset(token)


def _jsonable(data):
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def snapshot_fields(instance):
    exclude = TRACKING_FIELDS | set(getattr(instance, "version_exclude_fields", ()))
    for field in instance._meta.concrete_fields:
        if field.name not in exclude:
            yield field


def model_snapshot(instance):
    data = {}
    for field in snapshot_fields(instance):
        value = getattr(instance, field.attname)
        if isinstance(field, models.FileField):
            value = value.name if value else ""
        data[field.attname] = value
    return _jsonable(data)


def apply_model_snapshot(instance, data):
    for field in snapshot_fields(instance):
        if field.attname in data:
            value = data[field.attname]
            if not isinstance(field, (models.JSONField, models.FileField)) and value is not None:
                value = field.to_python(value)
            setattr(instance, field.attname, value)


def record_version(instance, change_type=None, comment=None):
    """Store a snapshot unless it is identical to the latest one. Returns the version or None."""
    from django.contrib.contenttypes.models import ContentType

    from .models import ConfigVersion

    ctx = _ctx.get() or {}
    if ctx.get("suppress"):
        return None
    snap = instance.snapshot()
    ct = ContentType.objects.get_for_model(instance.__class__)
    latest = (
        ConfigVersion.objects.filter(content_type=ct, object_id=instance.pk).order_by("-version").first()
    )
    change_type = change_type or ctx.get("change_type") or ("update" if latest else "create")
    if latest and latest.snapshot == snap and change_type in {"update", "create"}:
        return None
    if latest is None and change_type == "update":
        change_type = "create"
    return ConfigVersion.objects.create(
        content_type=ct,
        object_id=instance.pk,
        object_repr=str(instance)[:300],
        version=(latest.version + 1) if latest else 1,
        snapshot=snap,
        change_type=change_type,
        comment=(comment if comment is not None else ctx.get("comment", ""))[:500],
        created_by=get_current_user(),
    )


def versions_for(instance):
    from django.contrib.contenttypes.models import ContentType

    from .models import ConfigVersion

    ct = ContentType.objects.get_for_model(instance.__class__)
    return ConfigVersion.objects.filter(content_type=ct, object_id=instance.pk).order_by("-version")


@transaction.atomic
def rollback(instance, version):
    """Restore ``instance`` to the snapshot of ``version`` (recorded as a new version)."""
    instance.apply_snapshot(version.snapshot)
    with version_context(suppress=True):
        instance.full_clean(validate_unique=False)
        instance.save()
    record_version(instance, change_type="rollback", comment=f"Rolled back to v{version.version}")
    return instance


def _on_save(sender, instance, created, update_fields=None, **kwargs):
    from .models import VersionedMixin

    if not isinstance(instance, VersionedMixin) or kwargs.get("raw"):
        return
    if update_fields is not None:
        excluded = TRACKING_FIELDS | set(instance.version_exclude_fields)
        if set(update_fields) <= excluded:
            return
    record_version(instance)


def _on_delete(sender, instance, **kwargs):
    from .models import VersionedMixin

    if isinstance(instance, VersionedMixin):
        record_version(instance, change_type="delete", comment="Deleted (last state kept)")


def connect_signals():
    post_save.connect(_on_save, dispatch_uid="ewce_version_save")
    post_delete.connect(_on_delete, dispatch_uid="ewce_version_delete")
