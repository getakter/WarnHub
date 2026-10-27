"""Bangla / English text utilities: normalization, digits, language and legacy-font detection."""
import re
import unicodedata

BN_DIGITS = "০১২৩৪৫৬৭৮৯"
_TO_ASCII_DIGITS = str.maketrans(BN_DIGITS, "0123456789")
BN_RANGE = "ঀ-৿"
_BN_LETTER = re.compile(f"[{BN_RANGE}]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

# Characters that are meaningless in normal text and only add noise.
_STRIP_CHARS = dict.fromkeys(map(ord, "﻿­​⁠"), None)


def nfc(text):
    return unicodedata.normalize("NFC", text or "")


def to_ascii_digits(text):
    return (text or "").translate(_TO_ASCII_DIGITS)


def clean_text(text):
    """NFC, strip invisible noise, normalize whitespace; keeps ZWJ/ZWNJ (meaningful in Bangla)."""
    text = nfc(text).translate(_STRIP_CHARS)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def search_normalize(text):
    """Form used for searching: NFC, ASCII digits, casefolded, single spaces."""
    return re.sub(r"\s+", " ", to_ascii_digits(nfc(text)).casefold()).strip()


def script_counts(text):
    return len(_BN_LETTER.findall(text or "")), len(_LATIN_LETTER.findall(text or ""))


def detect_language(text):
    """Returns 'bn', 'en', 'mixed' or 'unknown' from the share of Bangla vs Latin letters."""
    bn, latin = script_counts(text)
    total = bn + latin
    if total < 10:
        return "unknown"
    ratio = bn / total
    if ratio >= 0.8:
        return "bn"
    if ratio <= 0.2:
        return "en"
    return "mixed"


# --- Legacy (ANSI / Bijoy / SutonnyMJ) Bangla detection -----------------------------
# Text typed in Bijoy-encoded fonts is stored as Latin-1 / Windows-1252 code points and
# looks like "evsjv‡`k AvenvIqv" once the font is gone. We look for the characteristic
# glyph substitutes plus very common Bijoy-encoded words.
LEGACY_SIGNATURE_CHARS = set("‡†ˆŠ‰‹Œ¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿ÖÜßœ›’–")
LEGACY_COMMON_WORDS = {
    "Ges", "Avi", "Kiv", "n‡e", "‡`k", "†`k", "evsjv", "evsjv‡`k", "Rb¨", "Ki‡Z", "n‡q‡Q",
    "AvenvIqv", "e„wó", "e„wócvZ", "eb¨v", "mZK©", "ms‡KZ", "c~e©vfvm", "ZvwiL", "wefvM",
    "†Rjv", "‡Rjv", "Dc‡Rjv", "N›Uvi", "g‡a¨", "cv‡i", "wewfbœ", "¯’v‡b", "Ges", "n‡Z",
}
LEGACY_FONT_PATTERN = re.compile(r"(bijoy|(?<!o)mj\b|(?<!o)mj[-_ ,]|ansi)", re.IGNORECASE)


def detect_legacy_bangla(text, font_names=()):
    """Return (is_legacy, score 0..1, reasons) for possible Bijoy/SutonnyMJ-encoded text."""
    reasons = []
    legacy_fonts = sorted({f for f in font_names if f and LEGACY_FONT_PATTERN.search(f)})
    tokens = (text or "").split()
    bn, latin = script_counts(text)
    if not tokens:
        return (bool(legacy_fonts), 1.0 if legacy_fonts else 0.0, ["legacy font: " + ", ".join(legacy_fonts)] if legacy_fonts else [])
    sig_tokens = sum(1 for t in tokens if any(c in LEGACY_SIGNATURE_CHARS for c in t))
    common = sum(1 for t in tokens if t.strip(".,:;|!?()") in LEGACY_COMMON_WORDS)
    sig_ratio = sig_tokens / len(tokens)
    mostly_unicode_bangla = bn > latin * 2
    score = min(1.0, sig_ratio * 2 + common * 0.1 + (0.5 if legacy_fonts else 0))
    if legacy_fonts:
        reasons.append("legacy font: " + ", ".join(legacy_fonts))
    if sig_ratio >= 0.12:
        reasons.append(f"{sig_ratio:.0%} of words contain Bijoy glyph substitutes")
    if common >= 3:
        reasons.append(f"{common} common Bijoy-encoded words")
    is_legacy = not mostly_unicode_bangla and (
        (legacy_fonts and latin > bn)
        or (len(tokens) >= 8 and sig_ratio >= 0.12)
        or common >= 3
    )
    return bool(is_legacy), round(score, 3), reasons
