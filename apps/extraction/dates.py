"""Date finding and parsing for Bangla, English and mixed text, with admin-editable formats."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

from apps.core.bangla import nfc, to_ascii_digits

from .rule_schemas import BANGLA_MONTHS, DEFAULT_DATE_FORMATS

_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_TIME = r"(?:[ ,T]+\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s*[ap]\.?m\.?)?)?"
CANDIDATES = [
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}" + _TIME, re.I),
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}" + _TIME, re.I),
    re.compile(r"(?:\b\d{1,2}[:.]\d{2}\s*(?:[ap]\.?m\.?)?,?\s+)?\b\d{1,2}(?:st|nd|rd|th)?[\s-]+" + _MONTHS
               + r"\.?,?[\s-]+\d{4}" + _TIME, re.I),
    re.compile(r"\b" + _MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}" + _TIME, re.I),
]
PDF_DATE = re.compile(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?")


def normalize_date_text(text, aliases=None):
    text = to_ascii_digits(nfc(text or ""))
    for word in sorted(aliases if aliases is not None else BANGLA_MONTHS, key=len, reverse=True):
        replacement = (aliases if aliases is not None else BANGLA_MONTHS)[word]
        text = text.replace(nfc(word), f" {replacement} " if replacement else " ")
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    text = re.sub(r"\b(\d{1,2})\.(\d{2})\s*([ap])\.?m\.?", r"\1:\2 \3m", text, flags=re.I)  # 9.30 a.m.
    text = re.sub(r"\b([ap])\.m\.?", r"\1m", text, flags=re.I)
    # "AM 9:30" (from Bangla সকাল ৯:৩০) -> "9:30 AM"; "9 AM" -> "9:00 AM"
    text = re.sub(r"\b(AM|PM)\s+(\d{1,2}(?::\d{2})?)", r"\2 \1", text, flags=re.I)
    text = re.sub(r"(?<![:\d])(\d{1,2})\s*(AM|PM)\b", r"\1:00 \2", text, flags=re.I)
    return " ".join(text.replace(" ,", ",").split())


def _to_aware(value, tz):
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(tz or "Asia/Dhaka"))
    return value


def parse_date(text, formats=None, aliases=None, tz="Asia/Dhaka"):
    """Parse a whole string with the given formats. Returns an aware datetime or None."""
    if not text:
        return None
    match = PDF_DATE.search(text)
    if match:
        parts = [int(p) if p else 0 for p in match.groups()]
        try:
            return _to_aware(dt.datetime(*parts), tz)
        except ValueError:
            return None
    normalized = normalize_date_text(text, aliases)
    variants = [normalized]
    if "," in normalized:
        variants.append(" ".join(normalized.replace(",", " ").split()))
    for variant in variants:
        for fmt in formats or DEFAULT_DATE_FORMATS:
            try:
                return _to_aware(dt.datetime.strptime(variant, fmt), tz)
            except ValueError:
                continue
    try:  # ISO 8601 with offset, e.g. RSS / meta tags
        return _to_aware(dt.datetime.fromisoformat(normalized.replace("Z", "+00:00")), tz)
    except ValueError:
        return None


def find_dates(text, formats=None, aliases=None, tz="Asia/Dhaka"):
    """All (candidate_text, datetime) found in free text, in order of appearance."""
    normalized = normalize_date_text(text, aliases)
    found = []
    for pattern in CANDIDATES:
        for match in pattern.finditer(normalized):
            candidate = match.group(0).strip(" ,")
            parsed = parse_date(candidate, formats, aliases={}, tz=tz)
            if parsed is None:  # retry without the time part
                parsed = parse_date(re.split(r"[ ,T]+\d{1,2}[:.]\d{2}", candidate)[0], formats, aliases={}, tz=tz)
            if parsed is not None:
                found.append((match.start(), candidate, parsed))
    found.sort(key=lambda f: f[0])
    seen, out = set(), []
    for _pos, candidate, parsed in found:
        if candidate not in seen:
            seen.add(candidate)
            out.append((candidate, parsed))
    return out
