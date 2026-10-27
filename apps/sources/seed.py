"""Example data. Everything created here is ordinary, editable and deletable (is_example=True).

Idempotent: existing objects (matched by code / slug / name) are never overwritten.
No page paths are invented: example feeds are disabled with empty entry URLs and a "configure me" note.
"""
from django.db import transaction

from apps.core.versioning import version_context

from .models import Feed, FeedKeywordSet, FormatProfile, Keyword, KeywordSet, Source
from .services import apply_profile_templates

_DOC_PATTERNS = ["*.pdf", "*.PDF", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.jpg", "*.jpeg", "*.png"]

_HTML_MAIN = {
    "name": "Main content", "rule_type": "html_selector", "applies_to_types": ["html"],
    "config": {"selector": "main, article, #content, .content, #main, body", "multiple": "first", "take": "text",
               "target": "body_text"},
}
_DATE_TEXT = {"name": "Published date (from text)", "rule_type": "date", "config": {"source": "text"}}
_PDF = {"name": "PDF text (OCR fallback)", "rule_type": "pdf", "applies_to_types": ["pdf"],
        "config": {"text_mode": "text_then_ocr", "layout": "plain", "remove_repeating_lines": True}}
_OFFICE = {"name": "Word / Excel", "rule_type": "office", "applies_to_types": ["docx", "xlsx", "xls", "csv"],
           "config": {"docx_include": "both", "header_row": 1, "row_format": "key_value"}}

PROFILES = [
    {
        "slug": "gov-listing-with-documents",
        "name": "Gov website listing with PDF/Word links",
        "description": "A notice/bulletin listing page whose links point to PDF, Word, Excel or image files. "
                       "Each linked document becomes a message.",
        "method": "listing_to_documents",
        "settings": {
            "files": {"allowed_types": ["html", "pdf", "docx", "doc", "xlsx", "xls", "image"]},
            "scope": {"include_patterns": _DOC_PATTERNS, "same_domain_only": True, "max_depth": 1,
                      "link_selector": "a[href]"},
            "dedupe": {"key_from": "url", "version_on_change": True},
        },
        "rules": [_PDF, _OFFICE, dict(_HTML_MAIN), _DATE_TEXT],
    },
    {
        "slug": "single-page-bulletin",
        "name": "Single-page bulletin",
        "description": "One web page that is updated in place (e.g. today's forecast). Changes create new versions.",
        "method": "web_page",
        "settings": {"files": {"allowed_types": ["html", "pdf"]}, "dedupe": {"key_from": "url", "version_on_change": True}},
        "rules": [dict(_HTML_MAIN), _DATE_TEXT],
    },
    {
        "slug": "notice-listing-rows",
        "name": "Notice listing (one message per row)",
        "description": "A table or list of notices where each row is itself the message (title, link, date).",
        "method": "web_page",
        "settings": {"files": {"allowed_types": ["html"]}, "dedupe": {"key_from": "url_title"}},
        "rules": [
            {"name": "Rows", "rule_type": "listing_items",
             "config": {"item_selector": "table tbody tr", "fields": {"title": "a", "link": "a@href", "date": "td:last-child"},
                        "skip_items_without": ["title"]}},
            {"name": "Row date", "rule_type": "date", "config": {"source": "field", "field": "date"}},
        ],
    },
    {
        "slug": "sectioned-bulletin",
        "name": "Bulletin split into sections",
        "description": "One page/document with several headed sections (e.g. per division). Each section is a message.",
        "method": "web_page",
        "settings": {"files": {"allowed_types": ["html", "pdf", "docx"]}, "dedupe": {"key_from": "url_title"}},
        "rules": [
            dict(_HTML_MAIN, config={**_HTML_MAIN["config"], "take": "inner_html"}),
            {"name": "Split by headings", "rule_type": "section_split",
             "config": {"split_by": "headings", "heading_tags": ["h2", "h3"], "heading_as_title": True}},
            _DATE_TEXT,
        ],
    },
    {
        "slug": "rss-atom",
        "name": "RSS / Atom feed",
        "description": "A standard RSS or Atom feed.",
        "method": "rss_atom",
        "settings": {"files": {"allowed_types": ["html"]}, "dedupe": {"key_from": "url"}},
        "rules": [{"name": "Entry date", "rule_type": "date", "config": {"source": "metadata"}}],
    },
    {
        "slug": "email-with-attachment",
        "name": "Email with PDF attachment",
        "description": "Bulletins received by email (body and/or PDF/Word attachments). Use with a mailbox rule.",
        "method": "email",
        "settings": {"files": {"allowed_types": ["pdf", "docx", "doc", "xlsx", "xls", "image", "txt", "html", "eml"]},
                     "dedupe": {"key_from": "content_hash"}},
        "rules": [{"name": "Email body", "rule_type": "email", "applies_to_types": ["eml"], "config": {}},
                  _PDF, _OFFICE, _DATE_TEXT],
    },
    {
        "slug": "manual-upload",
        "name": "Manual upload / paste",
        "description": "Operators upload files or paste text.",
        "method": "manual_upload",
        "settings": {"files": {"allowed_types": ["html", "pdf", "docx", "doc", "xlsx", "xls", "csv", "txt",
                                                 "rtf", "image", "eml", "zip"]},
                     "dedupe": {"key_from": "content_hash"}},
        "rules": [_PDF, _OFFICE, _DATE_TEXT],
    },
]

SOURCES = [
    {"code": "BMD", "name": "Bangladesh Meteorological Department", "base_url": "https://bmd.gov.bd",
     "default_language": "mixed", "color": "#0d6efd", "profile": "single-page-bulletin",
     "feed": "Weather forecast bulletin (example)"},
    {"code": "FFWC", "name": "Flood Forecasting and Warning Centre", "base_url": "https://www.ffwc.gov.bd",
     "default_language": "mixed", "color": "#0dcaf0", "profile": "gov-listing-with-documents",
     "feed": "Flood bulletins (example)"},
    {"code": "DDM", "name": "Department of Disaster Management", "base_url": "https://ddm.gov.bd",
     "default_language": "bn", "color": "#dc3545", "profile": "gov-listing-with-documents",
     "feed": "Disaster notices (example)"},
    {"code": "DAE", "name": "Department of Agricultural Extension", "base_url": "https://dae.gov.bd",
     "default_language": "bn", "color": "#198754", "profile": "notice-listing-rows",
     "feed": "Agro-advisories (example)"},
]

CONFIGURE_ME = (
    "CONFIGURE ME: example feed, disabled. Paste the real page URL into Entry URLs, use Inspect URL to find "
    "selectors, adjust the rules, Test feed, then enable. Preferred long-term: written permission or an "
    "agency email feed."
)

# (English term, English variants, Bangla term, Bangla variants, tag)
STARTER_KEYWORDS = [
    ("flood", ["floods", "flooding", "flash flood"], "বন্যা", ["বান", "জলাবদ্ধতা", "আকস্মিক বন্যা"], "flood"),
    ("cyclone", ["cyclonic", "depression", "tropical storm"], "ঘূর্ণিঝড়", ["ঘুর্ণিঝড়", "সাইক্লোন", "নিম্নচাপ", "ঘূর্ণিঝড়"], "cyclone"),
    ("heavy rainfall", ["heavy rain", "very heavy rainfall"], "ভারী বৃষ্টি", ["ভারি বৃষ্টি", "ভারী বর্ষণ", "অতি ভারী বৃষ্টিপাত", "ভারী বৃষ্টিপাত"], "rain"),
    ("heatwave", ["heat wave", "heat-wave"], "তাপপ্রবাহ", ["দাবদাহ", "তাপদাহ"], "heat"),
    ("lightning", ["thunderstorm", "thunder"], "বজ্রপাত", ["বজ্রসহ", "বজ্রঝড়"], "lightning"),
    ("signal", ["signal no", "danger signal"], "সংকেত", ["সতর্ক সংকেত", "বিপদ সংকেত", "মহাবিপদ সংকেত"], "signal"),
    ("warning", ["warnings", "alert"], "সতর্কতা", ["সতর্কবার্তা", "সতর্কবাণী", "সতর্ক"], "warning"),
    ("advisory", ["advisories", "advice"], "পরামর্শ", ["কৃষি পরামর্শ", "পরামর্শমালা"], "advisory"),
    ("forecast", ["forecasts", "outlook"], "পূর্বাভাস", ["পূর্বাভাষ", "আবহাওয়ার পূর্বাভাস"], "forecast"),
]


def seed_profiles():
    created = []
    for spec in PROFILES:
        if FormatProfile.objects.filter(slug=spec["slug"]).exists():
            continue
        profile = FormatProfile(
            slug=spec["slug"], name=spec["name"], description=spec["description"], method=spec["method"],
            settings=spec["settings"], rules_template=spec["rules"], is_example=True,
        )
        profile.full_clean()
        profile.save()
        created.append(profile)
    return created


def seed_keywords():
    kset, created = KeywordSet.objects.get_or_create(
        name="Starter hazard keywords (bilingual)",
        defaults={"description": "Example bilingual set. Edit freely; add spelling variants as you find them.",
                  "is_example": True},
    )
    if created:
        rows = []
        for en, en_variants, bn, bn_variants, tag in STARTER_KEYWORDS:
            rows.append(Keyword(keyword_set=kset, term=en, language="en", variants=en_variants,
                                match_mode="whole_word", tag=tag))
            rows.append(Keyword(keyword_set=kset, term=bn, language="bn", variants=bn_variants,
                                match_mode="prefix", tag=tag))
        for row in rows:
            row.normalize()
        Keyword.objects.bulk_create(rows)
        from apps.core.versioning import record_version

        record_version(kset, comment="Seeded keywords")
    return kset


def seed_sources(keyword_set):
    feeds = []
    for spec in SOURCES:
        source, _ = Source.objects.get_or_create(
            code=spec["code"],
            defaults={"name": spec["name"], "base_url": spec["base_url"], "default_language": spec["default_language"],
                      "color": spec["color"], "is_example": True,
                      "description": "Example source. Edit or delete freely.",
                      "permission_notes": "Record written permission / terms of use here."},
        )
        if source.feeds.exists():
            continue
        profile = FormatProfile.objects.get(slug=spec["profile"])
        feed = Feed(source=source, profile=profile, name=spec["feed"], slug="example", method=profile.method,
                    description=CONFIGURE_ME, is_active=False, tags=["example"], schedule_type="interval",
                    interval_minutes=180, expected_frequency_hours=24)
        feed.full_clean()
        feed.save()
        apply_profile_templates(feed, profile)
        FeedKeywordSet.objects.create(feed=feed, keyword_set=keyword_set)
        feeds.append(feed)
    return feeds


def seed_mailbox():
    from apps.mailbox.models import MailboxAccount, MailboxRule

    if MailboxAccount.objects.filter(label="Example agency mailbox").exists():
        return None
    mailbox = MailboxAccount(
        label="Example agency mailbox", protocol="imap", host="imap.example.org", port=993, security="ssl",
        username="alerts@example.org", password_env_var="EWCE_MAILBOX_EXAMPLE_PASSWORD", folders=["INBOX"],
        is_active=False, is_example=True, default_post_action="none",
    )
    mailbox.full_clean()
    mailbox.save()
    ffwc = Feed.objects.filter(source__code="FFWC").first()
    bmd = Feed.objects.filter(source__code="BMD").first()
    rules = []
    if ffwc:
        rules.append(MailboxRule(
            mailbox=mailbox, name="FFWC bulletins (PDF attachments)", position=10,
            from_patterns=["@ffwc.gov.bd"], has_attachment="yes", attachment_types=["pdf"],
            target_feed=ffwc, take="attachments", allowed_attachment_types=["pdf"], split="per_attachment",
        ))
    if bmd:
        rules.append(MailboxRule(
            mailbox=mailbox, name="BMD forecasts by subject", position=20, match_mode="all",
            from_patterns=["@bmd.gov.bd"], subject_contains=["forecast", "পূর্বাভাস", "warning", "সতর্কবার্তা"],
            target_feed=bmd, take="both", split="per_email",
        ))
    for rule in rules:
        rule.full_clean()
        rule.save()
    return mailbox


@transaction.atomic
def seed_all():
    with version_context(comment="Seed data"):
        profiles = seed_profiles()
        kset = seed_keywords()
        feeds = seed_sources(kset)
        mailbox = seed_mailbox()
    return {"profiles": len(profiles), "feeds": len(feeds), "mailbox": bool(mailbox)}
