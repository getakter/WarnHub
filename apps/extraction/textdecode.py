"""Bytes -> str with an admin override, declared charsets, BOMs and detection as fallbacks."""
import codecs
import re

from charset_normalizer import from_bytes

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_\-:.]+)""", re.I)


def _valid(name):
    try:
        codecs.lookup(name)
        return True
    except (LookupError, TypeError):
        return False


def declared_charset(data, content_type=""):
    match = re.search(r"charset=([\w\-:.]+)", content_type or "", re.I)
    if match and _valid(match.group(1)):
        return match.group(1)
    match = _META_CHARSET.search(data[:4096])
    if match and _valid(match.group(1).decode("ascii", "ignore")):
        return match.group(1).decode("ascii")
    return None


def decode_bytes(data, *, override="", content_type="", warnings=None):
    """Return (text, encoding_used)."""
    if override and _valid(override):
        return data.decode(override, errors="replace"), override
    for bom, name in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data.decode(name, errors="replace"), name
    declared = declared_charset(data, content_type)
    candidates = [declared] if declared else []
    candidates.append("utf-8")
    for name in candidates:
        try:
            return data.decode(name), name
        except (UnicodeDecodeError, LookupError):
            continue
    best = from_bytes(data).best()
    if best is not None:
        if warnings is not None:
            warnings.append(f"Encoding guessed as {best.encoding}; set an encoding override if text looks wrong.")
        return str(best), best.encoding
    if warnings is not None:
        warnings.append("Could not detect the encoding; decoded as UTF-8 with replacement characters.")
    return data.decode("utf-8", errors="replace"), "utf-8"
