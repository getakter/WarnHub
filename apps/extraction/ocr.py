"""Optional OCR via Tesseract (feature flag EWCE_ENABLE_OCR). Degrades to 'unavailable' when absent."""
import io
import logging
import shutil
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger("ewce.ocr")


@lru_cache(maxsize=1)
def _status():
    if not settings.EWCE_ENABLE_OCR:
        return False, "OCR is disabled (EWCE_ENABLE_OCR=False)"
    try:
        import pytesseract
    except ImportError:
        return False, "pytesseract is not installed"
    cmd = settings.EWCE_TESSERACT_CMD or shutil.which("tesseract")
    if not cmd:
        return False, "Tesseract executable not found (set EWCE_TESSERACT_CMD)"
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # noqa: BLE001
        return False, f"Tesseract not usable: {exc}"
    missing = [lang for lang in settings.EWCE_OCR_LANG.split("+") if lang not in langs]
    if missing:
        return False, f"Tesseract language data missing: {', '.join(missing)}"
    return True, f"Tesseract OK ({settings.EWCE_OCR_LANG})"


def ocr_available():
    return _status()[0]


def ocr_status_message():
    return _status()[1]


def ocr_image_bytes(data):
    import pytesseract
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return pytesseract.image_to_string(image, lang=settings.EWCE_OCR_LANG)


def ocr_pdf_page(page, dpi=300):
    """OCR one PyMuPDF page."""
    pixmap = page.get_pixmap(dpi=dpi)
    return ocr_image_bytes(pixmap.tobytes("png"))
