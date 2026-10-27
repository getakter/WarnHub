import unicodedata

from apps.core.bangla import (
    clean_text, detect_language, detect_legacy_bangla, nfc, search_normalize, to_ascii_digits,
)

PRECOMPOSED = "ঘূর্ণিঝড়"  # ড় as the single code point U+09DC
DECOMPOSED = "ঘূর্ণিঝড়"  # ড + nukta, as many keyboards type it


def test_nfc_unifies_bangla_code_point_orders():
    assert PRECOMPOSED != DECOMPOSED
    assert nfc(PRECOMPOSED) == nfc(DECOMPOSED)
    # U+09DC is a composition exclusion: NFC is the decomposed form
    assert unicodedata.normalize("NFC", PRECOMPOSED) == DECOMPOSED


def test_bangla_digits_to_ascii():
    assert to_ascii_digits("তারিখ: ২৭/১০/২০২৬ সময় ০৯:৩০") == "তারিখ: 27/10/2026 সময় 09:30"


def test_clean_text_keeps_zwnj_and_normalizes_whitespace():
    text = "র‌য়  বন্যা\r\n\r\n\r\n\r\nসতর্কতা﻿"
    cleaned = clean_text(text)
    assert "‌" in cleaned
    assert "﻿" not in cleaned
    assert cleaned.endswith("বন্যা\n\nসতর্কতা")


def test_search_normalize():
    assert search_normalize("Signal No. ৩ ঘূর্ণিঝড়") == f"signal no. 3 {nfc('ঘূর্ণিঝড়')}"


def test_detect_language():
    assert detect_language("আগামী ২৪ ঘণ্টায় দেশের বিভিন্ন স্থানে ভারী বৃষ্টি হতে পারে") == "bn"
    assert detect_language("Heavy rainfall is likely at places over the country") == "en"
    assert detect_language("Cyclone signal ৩ জারি করা হয়েছে for Chattogram port") == "mixed"
    assert detect_language("ok") == "unknown"


BIJOY_SAMPLE = (
    "evsjv‡`k AvenvIqv Awa`ß‡ii c~e©vfvm: AvMvgx 24 N›Uvi g‡a¨ †`‡ki wewfbœ ¯’v‡b "
    "e„wócvZ n‡Z cv‡i Ges ‡Kv_vI ‡Kv_vI fvix e„wócvZ n‡Z cv‡i|"
)


def test_legacy_bijoy_text_is_detected():
    is_legacy, score, reasons = detect_legacy_bangla(BIJOY_SAMPLE)
    assert is_legacy
    assert score > 0.3
    assert reasons


def test_legacy_font_name_detected_in_pdf_fonts():
    is_legacy, _, reasons = detect_legacy_bangla("Kiv n‡e Ges", font_names=["ABCDEF+SutonnyMJ"])
    assert is_legacy
    assert any("SutonnyMJ" in r for r in reasons)


def test_unicode_bangla_and_english_are_not_legacy():
    assert not detect_legacy_bangla("আগামী ২৪ ঘণ্টায় দেশের বিভিন্ন স্থানে ভারী বৃষ্টি হতে পারে।")[0]
    assert not detect_legacy_bangla("Heavy rainfall is likely at places over Dhaka and Sylhet divisions.")[0]
    # SutonnyOMJ is the Unicode-compliant version and must not be flagged
    assert not detect_legacy_bangla("বন্যা পরিস্থিতি", font_names=["SutonnyOMJ"])[0]
