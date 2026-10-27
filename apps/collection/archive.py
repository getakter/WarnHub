"""Immutable, content-addressed raw archive: data/artifacts/ab/cd/<sha256><ext>."""
import hashlib
import os
import stat
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.extraction.filetypes import EXTENSION_FOR_TYPE, extension_type

from .models import RawArtifact

MAX_HEADER_CHARS = 2000


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _extension(file_type, filename):
    if file_type == "image" and extension_type(filename) == "image":
        return os.path.splitext(filename)[1].lower()
    return EXTENSION_FOR_TYPE.get(file_type, ".bin")


def _write_once(path, data):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)  # read-only on disk


def clean_headers(headers):
    return {str(k): str(v)[:MAX_HEADER_CHARS] for k, v in (headers or {}).items()}


def store_artifact(data, *, file_type, filename="", content_type="", source_url="", headers=None,
                   origin="http", parent=None, feed=None, run=None):
    """Return (artifact, created). Identical bytes are stored once; later sightings bump seen_count."""
    digest = sha256_hex(data)
    relative = f"{digest[:2]}/{digest[2:4]}/{digest}{_extension(file_type, filename)}"
    existing = RawArtifact.objects.filter(sha256=digest).first()
    if existing is not None:
        _write_once(settings.EWCE_ARTIFACT_ROOT / existing.storage_path, data)  # self-heal a missing file
        RawArtifact.objects.filter(pk=existing.pk).update(seen_count=F("seen_count") + 1, last_seen_at=timezone.now())
        return existing, False
    _write_once(settings.EWCE_ARTIFACT_ROOT / relative, data)
    try:
        with transaction.atomic():
            artifact = RawArtifact.objects.create(
                sha256=digest, size=len(data), content_type=(content_type or "")[:200], file_type=file_type,
                original_filename=(filename or "")[:500], storage_path=relative, source_url=(source_url or "")[:2000],
                response_headers=clean_headers(headers), origin=origin, parent=parent, first_feed=feed, first_run=run,
            )
    except IntegrityError:  # stored concurrently by another process
        return RawArtifact.objects.get(sha256=digest), False
    return artifact, True


def read_artifact(artifact):
    return (settings.EWCE_ARTIFACT_ROOT / artifact.storage_path).read_bytes()
