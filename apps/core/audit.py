import logging

from .middleware import get_current_user
from .models import AuditLog

logger = logging.getLogger("ewce.audit")


def audit(action, obj=None, user=None, **details):
    user = user or get_current_user()
    entry = AuditLog.objects.create(
        user=user,
        action=action,
        object_type=obj._meta.label if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        object_repr=str(obj)[:300] if obj is not None else "",
        details=details,
    )
    logger.info("action=%s object=%r user=%s details=%s", action, entry.object_repr, user, details)
    return entry
