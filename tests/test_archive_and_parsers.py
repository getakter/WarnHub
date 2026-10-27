import os
import stat

import pytest

from apps.collection.archive import read_artifact, store_artifact
from apps.extraction.filetypes import detect_file_type
from apps.extraction.parsers import (
    DocxParser, EmlParser, HtmlParser, ImageParser, PdfParser, RtfParser, SpreadsheetParser, TxtParser, ZipParser,
    parse_page_range,
)
from tests.helpers import make_docx, make_eml, make_pdf, make_xlsx, make_zip

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


@pytest.mark.django_db
def test_archive_is_content_addressed_and_immutable(artifact_root):
    a1, created1 = store_artifact(b"hello bulletin", file_type="txt", filename="a.txt", source_url="https://x/a.txt",
                                  headers={"Content-Type": "text/plain"})
    a2, created2 = store_artifact(b"hello bulletin", file_type="txt", filename="b.txt")
    assert created1 and not created2 and a1.pk == a2.pk
    a1.refresh_from_db()
    assert a1.seen_count == 2
    assert a1.storage_path.startswith(f"{a1.sha256[:2]}/{a1.sha256[2:4]}/{a1.sha256}")
    path = artifact_root / a1.storage_path
    assert read_artifact(a1) == b"hello bulletin"
    assert not os.stat(path).st_mode & stat.S_IWRITE  # read-only on disk
    assert a1.response_headers == {"Content-Type": "text/plain"}


@pytest.mark.parametrize("data,name,ctype,expected", [
    (b"%PDF-1.7 ...", "", "", "pdf"),
    (None, "x.docx", "", "docx"),
    (None, "x.xlsx", "", "xlsx"),
    (b"PK\x03\x04" + b"\x00" * 30, "x.zip", "", "zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20, "old.xls", "", "xls"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20, "old.doc", "", "doc"),
    (b"{\\rtf1\\ansi hello}", "", "", "rtf"),
    (PNG, "", "", "image"),
    (b"<!DOCTYPE html><html><body>hi</body></html>", "", "", "html"),
    (b"<div>no doctype</div><body>x</body>", "", "text/html; charset=utf-8", "html"),
    ("From: a@b\nTo: c@d\nSubject: x\nMessage-ID: <1>\n\nbody".encode(), "", "", "eml"),
    ("আগামী ২৪ ঘণ্টায় বৃষ্টি".encode(), "", "", "txt"),
    (b"a,b\n1,2", "data.csv", "", "csv"),
    (b'{"data": {"Scroll": []}}', "", "text/html; charset=UTF-8", "json"),
    (b"[1, 2]", "", "application/json", "json"),
    (b"[not json", "", "", "txt"),
    (b"\x00\x01\x02\x03\x04" * 50, "", "", "unknown"),
])
def test_detect_file_type(data, name, ctype, expected):
    if data is None:
        data = make_docx(["x"]) if expected == "docx" else make_xlsx({"S": [["a"]]})
    assert detect_file_type(data, name, ctype) == expected


def test_html_parser_decodes_and_strips_noise():
    html = ('<html><head><meta charset="windows-1252"><title>Daily Forecast</title>'
            '<meta name="date" content="2026-10-27"></head><body><script>alert(1)</script>'
            '<p>Temperature 30\xb0C</p><a href="/b/1.pdf">Bulletin 1</a></body></html>').encode("cp1252")
    doc = HtmlParser().parse(html, url="https://bmd.gov.bd/forecast/")
    assert doc.title == "Daily Forecast"
    assert "Temperature 30°C" in doc.text and "alert" not in doc.text
    assert doc.links == [{"url": "https://bmd.gov.bd/b/1.pdf", "text": "Bulletin 1"}]
    assert doc.metadata["date"] == "2026-10-27"


def test_html_encoding_override():
    data = "বন্যা".encode("utf-8")
    doc = HtmlParser().parse(b"<html><body>" + data + b"</body></html>", settings={"content": {"encoding": "utf-8"}})
    assert "বন্যা" in doc.text


def test_page_range():
    assert parse_page_range("", 4) == [0, 1, 2, 3]
    assert parse_page_range("1-2,4", 5) == [0, 1, 3]
    assert parse_page_range("-2", 5) == [3, 4]
    assert parse_page_range("2,2,9", 3) == [1]


def test_pdf_text_page_range_and_repeating_header_removal():
    stations = ["Sirajganj", "Bahadurabad", "Sunamganj", "Kurigram"]
    pages = [
        f"FFWC Daily Bulletin\nRiver level report page {i}\n{stations[i - 1]} station is {i}0 cm above danger\n"
        f"Trend: rising at {stations[i - 1]}\nOutlook for {stations[i - 1]} next 24h\nPage {i} of 4"
        for i in range(1, 5)
    ]
    data = make_pdf(pages)
    doc = PdfParser().parse(data, options={"page_range": "1-3"})
    assert "Sirajganj station is 10 cm" in doc.text and "Kurigram" not in doc.text
    assert "FFWC Daily Bulletin" not in doc.text  # repeated on every page -> header
    assert "Page 2 of 4" not in doc.text  # numbers normalized when spotting repeats
    assert doc.pages == 4 and doc.font_names
    kept = PdfParser().parse(data, options={"remove_repeating_lines": False, "remove_line_patterns": [r"^Page \d"]})
    assert "FFWC Daily Bulletin" in kept.text and "Page 1 of 4" not in kept.text


def test_pdf_without_text_layer_flags_ocr_unavailable():
    import fitz

    pdf = fitz.open()
    pdf.new_page()
    doc = PdfParser().parse(pdf.tobytes())
    assert "ocr_unavailable" in doc.quality_flags and doc.attention


def test_docx_paragraphs_tables_and_bangla():
    data = make_docx(["ঘূর্ণিঝড় সতর্কবার্তা", "Signal 3"], table=[["Port", "Signal"], ["Chattogram", "৩"]],
                     heading="Cyclone advisory")
    doc = DocxParser().parse(data)
    assert doc.title == "Cyclone advisory"
    assert "ঘূর্ণিঝড় সতর্কবার্তা" in doc.text and "Chattogram | ৩" in doc.text
    tables_only = DocxParser().parse(data, options={"docx_include": "tables"})
    assert "Signal 3" not in tables_only.text and "Port | Signal" in tables_only.text


def test_xlsx_rows_to_text_and_segments():
    data = make_xlsx({"Rivers": [["Station", "Level", "Danger"], ["Sirajganj", 13.2, 13.35], ["Bahadurabad", 19.5, 19.5]],
                      "Notes": [["n"], ["ignore me"]]})
    parser = SpreadsheetParser()
    parser.file_type_hint = "xlsx"
    doc = parser.parse(data, options={"sheets": ["Rivers"], "rows_as_items": True})
    assert "Station: Sirajganj; Level: 13.2; Danger: 13.35" in doc.text and "ignore me" not in doc.text
    assert len(doc.segments) == 2 and doc.segments[1]["fields"]["Station"] == "Bahadurabad"
    ranged = parser.parse(data, options={"sheets": ["1"], "row_range": "3-3", "row_format": "tab", "column_range": "A-B"})
    assert ranged.text == "Bahadurabad\t19.5"


def test_csv_bangla_semicolon():
    parser = SpreadsheetParser()
    parser.file_type_hint = "csv"
    doc = parser.parse("জেলা;সতর্কতা\nসিলেট;বন্যা\n".encode("utf-8"))
    assert "জেলা: সিলেট; সতর্কতা: বন্যা" in doc.text


def test_txt_rtf_and_image():
    assert TxtParser().parse("বন্যা পরিস্থিতি\r\n\r\n\r\n\r\nউন্নতি".encode("utf-16")).text == "বন্যা পরিস্থিতি\n\nউন্নতি"
    rtf = RtfParser().parse(b"{\\rtf1\\ansi{\\fonttbl{\\f0\\fnil SutonnyMJ;}}\\f0 Kiv n\\'8ce Ges}")
    assert "SutonnyMJ" in rtf.font_names and "Kiv" in rtf.text
    image = ImageParser().parse(PNG)
    assert "ocr_unavailable" in image.quality_flags and image.attention


def test_eml_bangla_subject_body_attachments_and_stripping():
    body = "আগামী ২৪ ঘণ্টায় নদীর পানি বাড়তে পারে।\n\nধন্যবাদান্তে\nFFWC Desk\n\n> old quoted text"
    data = make_eml("বন্যা পূর্বাভাস / Flood forecast", body,
                    attachments=[("bulletin.pdf", make_pdf(["Water level rising"]), "application", "pdf")])
    doc = EmlParser().parse(data)
    assert doc.title == "বন্যা পূর্বাভাস / Flood forecast"
    assert doc.text == "আগামী ২৪ ঘণ্টায় নদীর পানি বাড়তে পারে।"
    assert doc.metadata["email.from"] == "ffwc@ffwc.gov.bd"
    assert [a["filename"] for a in doc.attachments] == ["bulletin.pdf"]
    html_doc = EmlParser().parse(make_eml("x", "plain", html="<p>HTML <b>body</b></p>"), options={"body_source": "prefer_html"})
    assert html_doc.text == "HTML\nbody" or html_doc.text == "HTML body"


def test_zip_members():
    data = make_zip({"a.txt": "one", "dir/b.txt": "two", "__MACOSX/._a.txt": "junk"})
    doc = ZipParser().parse(data)
    assert [a["filename"] for a in doc.attachments] == ["a.txt", "dir/b.txt"]
