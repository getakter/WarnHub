"""OCR fallback (feature-flagged) and legacy Bangla font handling on real documents."""
import pytest

from apps.collection.models import CollectedMessage
from apps.collection.normalize import finalize_item
from apps.collection.pipeline import ingest_upload
from apps.extraction import ocr
from apps.extraction.base import Item, ParsedDocument
from apps.extraction.parsers import ImageParser, PdfParser
from apps.sources.config import builtin_defaults
from tests.helpers import make_pdf

BIJOY_LINES = [
    "evsjv‡`k AvenvIqv Awa`ß‡ii c~e©vfvm",
    "AvMvgx 24 N›Uvi g‡a¨ †`‡ki wewfbœ ¯’v‡b",
    "e„wócvZ n‡Z cv‡i Ges ‡Kv_vI ‡Kv_vI fvix e„wócvZ n‡Z cv‡i|",
]


@pytest.fixture
def fake_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_pdf_page", lambda page, dpi=300: "OCR পাঠ্য: সিলেটে বন্যা পরিস্থিতি")
    monkeypatch.setattr(ocr, "ocr_image_bytes", lambda data: "ছবি থেকে পাঠ্য: বজ্রপাত সতর্কতা")


def test_scanned_pdf_uses_ocr_when_enabled(fake_ocr):
    import fitz

    pdf = fitz.open()
    pdf.new_page()  # no text layer = scanned
    doc = PdfParser().parse(pdf.tobytes())
    assert "ocr_used" in doc.quality_flags and "সিলেটে বন্যা" in doc.text and not doc.attention


def test_text_layer_pdf_does_not_need_ocr(fake_ocr):
    doc = PdfParser().parse(make_pdf(["A proper text layer with enough characters to skip OCR."]))
    assert "ocr_used" not in doc.quality_flags


def test_forced_ocr_mode_and_images(fake_ocr):
    doc = PdfParser().parse(make_pdf(["Text layer"]), options={"text_mode": "ocr"})
    assert "ocr_used" in doc.quality_flags and "OCR" in doc.text
    image = ImageParser().parse(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    assert image.text == "ছবি থেকে পাঠ্য: বজ্রপাত সতর্কতা" and "ocr_used" in image.quality_flags


def test_ocr_disabled_by_default_status_message():
    ocr._status.cache_clear()
    assert ocr.ocr_available() is False and "disabled" in ocr.ocr_status_message()


@pytest.mark.django_db
def test_garbled_legacy_pdf_is_flagged_not_stored_as_clean_text(feed):
    ingest_upload(feed, files=[("sutonny-bulletin.pdf", make_pdf(BIJOY_LINES), "application/pdf")])
    message = CollectedMessage.objects.get()
    assert message.status == "needs_attention"
    assert "legacy_bangla_font" in message.quality_flags
    assert message.body_text == "" and "AvenvIqv" in message.raw_extracted_text
    assert message.raw_artifact.file_type == "pdf"  # the original is kept for conversion later


def test_legacy_font_name_in_pdf_flags_item():
    document = ParsedDocument(file_type="pdf", font_names=["ABCDEF+SutonnyMJ", "Arial"])
    item = Item(title="", body_text="Kiv n‡e Ges Avi ‡`k")
    finalize_item(item, document, builtin_defaults(), filename="b.pdf")
    assert "legacy_bangla_font" in item.quality_flags and item.body_text == "" and item.title == "b.pdf"


def test_unicode_bangla_pdf_text_is_clean():
    document = ParsedDocument(file_type="pdf", font_names=["NikoshBAN", "SolaimanLipi"])
    item = Item(title="আবহাওয়া বার্তা", body_text="আগামী ২৪ ঘণ্টায় ভারী বৃষ্টি হতে পারে।")
    finalize_item(item, document, builtin_defaults())
    assert "legacy_bangla_font" not in item.quality_flags and item.body_text
