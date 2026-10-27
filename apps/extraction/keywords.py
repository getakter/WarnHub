"""Keyword matching (Bangla-aware word boundaries) and capture-mode decisions."""
import re

from apps.core.bangla import nfc, search_normalize, to_ascii_digits

# A "letter" for boundary purposes: word chars plus the whole Bangla block (vowel signs, virama...).
_LETTER = r"[\wঀ-৿]"


class CompiledKeyword:
    def __init__(self, keyword, set_name):
        self.keyword = keyword
        self.set_name = set_name
        self.patterns = [self._compile(term) for term in [keyword.term, *(keyword.variants or [])] if term]

    def _normalize(self, term):
        return to_ascii_digits(nfc(term)) if self.keyword.case_sensitive else search_normalize(term)

    def _compile(self, term):
        mode = self.keyword.match_mode
        flags = 0 if self.keyword.case_sensitive else re.IGNORECASE
        if mode == "regex":
            return term, re.compile(nfc(term), flags)
        body = re.escape(self._normalize(term)).replace(r"\ ", r"\s+")
        if mode == "whole_word":
            body = rf"(?<!{_LETTER}){body}(?!{_LETTER})"
        elif mode == "prefix":
            body = rf"(?<!{_LETTER}){body}"
        return term, re.compile(body, flags)

    def search(self, text_normalized, text_nfc):
        text = text_nfc if (self.keyword.case_sensitive or self.keyword.match_mode == "regex") else text_normalized
        for term, pattern in self.patterns:
            found = pattern.search(text)
            if found:
                return term, found.group(0)
        return None


class KeywordMatcher:
    def __init__(self, keyword_sets):
        self.keywords = []
        for kset in keyword_sets:
            for keyword in kset.keywords.all():
                if keyword.enabled:
                    self.keywords.append(CompiledKeyword(keyword, kset.name))

    @property
    def empty(self):
        return not self.keywords

    def match(self, text):
        text_nfc = to_ascii_digits(nfc(text))
        text_normalized = search_normalize(text)
        hits = []
        for compiled in self.keywords:
            found = compiled.search(text_normalized, text_nfc)
            k = compiled.keyword
            hits.append({
                "term": k.term, "variant": found[0] if found else "", "matched": found[1] if found else "",
                "set": compiled.set_name, "kind": k.kind, "tag": k.tag, "weight": k.weight, "hit": bool(found),
            })
        return hits


def evaluate(hits, mode):
    """Return (keep, flag, reason, matched_list, score) for a capture mode."""
    matched = [{k: v for k, v in h.items() if k != "hit"} for h in hits if h["hit"]]
    includes = [h for h in hits if h["kind"] == "include"]
    excluded = [h for h in hits if h["kind"] == "exclude" and h["hit"]]
    missing_required = [h for h in hits if h["kind"] == "required" and not h["hit"]]
    score = sum(h["weight"] for h in hits if h["hit"] and h["kind"] != "exclude")
    reasons = []
    if excluded:
        reasons.append("exclude keyword: " + ", ".join(h["term"] for h in excluded))
    if missing_required:
        reasons.append("required keyword missing: " + ", ".join(h["term"] for h in missing_required))
    if includes and not any(h["hit"] for h in includes):
        reasons.append("no include keyword matched")
    if not hits:
        reasons.append("no keyword sets assigned to this feed")
    passes = not reasons
    if mode == "capture_all" or passes:
        return True, None, "", matched, score
    if mode == "keyword_match_or_tag":
        return True, "no_keyword_match", "; ".join(reasons), matched, score
    return False, None, "; ".join(reasons), matched, score
