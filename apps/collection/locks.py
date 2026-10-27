"""Per-feed / per-mailbox locks in the database, so runs never overlap (even across processes)."""
import os
import socket
import uuid
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

DEFAULT_TTL = timedelta(minutes=30)


def new_owner(prefix="run"):
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:100]


def acquire(model, pk, owner, ttl=DEFAULT_TTL):
    now = timezone.now()
    free = Q(lock_owner="") | Q(lock_expires_at__isnull=True) | Q(lock_expires_at__lt=now)
    return model.objects.filter(pk=pk).filter(free).update(lock_owner=owner, lock_expires_at=now + ttl) == 1


def release(model, pk, owner):
    model.objects.filter(pk=pk, lock_owner=owner).update(lock_owner="", lock_expires_at=None)


def extend(model, pk, owner, ttl=DEFAULT_TTL):
    model.objects.filter(pk=pk, lock_owner=owner).update(lock_expires_at=timezone.now() + ttl)
