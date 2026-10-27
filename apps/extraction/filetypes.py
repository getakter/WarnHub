"""Detect our normalized file type from bytes, file name and Content-Type."""
import io
import json
import mimetypes
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

EXTENSIONS = {
    ".html": "html", ".htm": "html", ".xhtml": "html", ".php": "html", ".aspx": "html", ".asp": "html", ".jsp": "html",
    ".pdf": "pdf", ".docx": "docx", ".doc": "doc", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls",
    ".csv": "csv", ".json": "json", ".txt": "txt", ".text": "txt", ".rtf": "rtf", ".eml": "eml", ".zip": "zip",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".tif": "image", ".tiff": "image",
    ".webp": "image", ".bmp": "image",
}
CONTENT_TYPES = {
    "text/html": "html", "application/xhtml+xml": "html", "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls", "text/csv": "csv", "application/json": "json", "text/plain": "txt",
    "application/rtf": "rtf", "text/rtf": "rtf", "message/rfc822": "eml",
    "application/zip": "zip", "application/x-zip-compressed": "zip",
}
EXTENSION_FOR_TYPE = {
    "html": ".html", "pdf": ".pdf", "docx": ".docx", "doc": ".doc", "xlsx": ".xlsx", "xls": ".xls", "csv": ".csv",
    "json": ".json", "txt": ".txt", "rtf": ".rtf", "eml": ".eml", "zip": ".zip", "image": ".img", "unknown": ".bin",
}
_EML_HEADER = re.compile(rb"^(Received|From|Return-Path|Message-ID|MIME-Version|Delivered-To|Subject|Date):", re.I | re.M)


def filename_from_url(url):
    if not url:
        return ""
    name = PurePosixPath(unquote(urlparse(url).path)).name
    return name if "." in name else ""


def extension_type(name):
    suffix = PurePosixPath((name or "").lower()).suffix
    return EXTENSIONS.get(suffix)


def _zip_type(data):
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return "zip"


def detect_file_type(data, filename="", content_type="", url=""):
    """Magic bytes first, then Content-Type, then extension. Returns one of FILE_TYPES or 'unknown'."""
    head = data[:2048]
    ctype = (content_type or "").split(";")[0].strip().lower()
    by_ext = extension_type(filename) or extension_type(filename_from_url(url))
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return _zip_type(data) or "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # OLE2: .doc or .xls
        if by_ext == "xls" or CONTENT_TYPES.get(ctype) == "xls":
            return "xls"
        return "doc"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG") or head.startswith(b"GIF8")
            or head[:4] in (b"II*\x00", b"MM\x00*") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head.startswith(b"BM")):
        return "image"
    if ctype.startswith("image/"):
        return "image"
    sniff = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if sniff.startswith((b"<!doctype html", b"<html", b"<?xml")) and (b"<html" in head.lower() or b"<body" in head.lower()):
        return "html"
    if by_ext == "eml" or ctype == "message/rfc822" or (len(_EML_HEADER.findall(head)) >= 3 and by_ext is None):
        return "eml"
    if sniff.startswith((b"{", b"[")) and _is_json(data):  # API responses are often served as text/html
        return "json"
    if by_ext in {"csv", "txt", "html", "eml", "json"}:
        return by_ext
    if ctype in CONTENT_TYPES and CONTENT_TYPES[ctype] not in {"zip", "pdf", "docx", "xlsx", "doc", "xls"}:
        return CONTENT_TYPES[ctype]
    if b"<html" in head.lower() or b"<body" in head.lower():
        return "html"
    if by_ext:
        return by_ext
    if _looks_like_text(head):
        return "txt"
    return "unknown"


def _is_json(data):
    try:
        json.loads(data.decode("utf-8-sig"))
        return True
    except (UnicodeDecodeError, ValueError):
        return False


def _looks_like_text(head):
    if not head:
        return False
    if b"\x00" in head and not (head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff")):
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        control = sum(1 for b in head if b < 9 or 13 < b < 32)
        return control / len(head) < 0.05


def guess_content_type(file_type, filename=""):
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed
    reverse = {v: k for k, v in CONTENT_TYPES.items()}
    return reverse.get(file_type, "application/octet-stream")
