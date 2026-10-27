"""URL / text pattern matching shared by rules and URL scope: wildcard (default) or regex."""
import fnmatch
import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern, syntax):
    if syntax == "regex" or pattern.startswith("re:"):
        return re.compile(pattern[3:] if pattern.startswith("re:") else pattern, re.IGNORECASE)
    return re.compile(fnmatch.translate(pattern), re.IGNORECASE)


def matches(pattern, value, syntax="wildcard"):
    """Wildcards match the whole value (``*`` = anything); regexes search anywhere."""
    compiled = _compile(pattern, syntax)
    if syntax == "regex" or pattern.startswith("re:"):
        return bool(compiled.search(value or ""))
    return bool(compiled.match(value or ""))


def matches_any(patterns, value, syntax="wildcard"):
    return any(matches(p, value, syntax) for p in patterns or [])
