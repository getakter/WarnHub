# WarnHub — Scope 1 Architecture

_WarnHub was developed under the working name "Early Warning Collection Engine (EWCE)"; internal identifiers
(`ewce` package and database, `EWCE_*` settings, the `ewce-config` export format id) keep that name._

## 1. Environment & versions (checked on the target laptop)

| Component | Found | Decision |
|---|---|---|
| Python | 3.13.5 | venv in `.venv`, pinned `requirements.txt` |
| DB | XAMPP MariaDB **10.4.32**, utf8mb4 | utf8mb4 / `utf8mb4_unicode_ci` on DB, tables and connection |
| Django | — | **Django 5.2 LTS** (security support to Apr 2028) |
| Driver | — | `mysqlclient` 2.3 (Windows wheel for cp313) |

**Why not Django 4.2?** It supports MariaDB 10.4, but 4.2 LTS reached end-of-life in April 2026
(no more security fixes) and never officially supported Python 3.13. Django 5.2 refuses
MariaDB < 10.5 only via one version gate (`minimum_database_version`); every 10.5-only behaviour
(e.g. `INSERT … RETURNING`) is feature-detected from the live server version. We ship a 10-line
backend `ewce.db.mariadb_compat` that lowers only that gate, and the whole test suite runs on the
real 10.4 server. **Recommended:** upgrade to MariaDB 10.11 LTS when convenient, then set
`DB_ENGINE=django.db.backends.mysql` in `.env`.

**`max_allowed_packet` is 1 MB in XAMPP's `[mysqld]` section.** Long bulletins can exceed this.
Setup raises it to 64M; a Django system check and a dashboard banner warn if it is still low, and
bodies longer than `EWCE_MAX_BODY_CHARS` are truncated with the `truncated` quality flag (the raw
artifact always keeps the full original).

Optional (feature-flagged, degrade gracefully): Tesseract OCR (`ben+eng`) — not installed;
Playwright JS rendering — not installed.

## 2. Folder structure

```
Songket/
  manage.py  requirements.txt  requirements-optional.txt  .env.example  pytest.ini
  ewce/                    project settings, urls, logging, MariaDB 10.4 compat backend
  apps/
    core/                  TrackedModel (created/updated/by), ConfigVersion, AuditLog, current-user
                           middleware, Bangla text utils, deep-merge, roles, system checks
    plugins/               plugin registry, JSON-Schema validation, schema -> Django form generator,
                           custom plugin loader (custom_plugins/ folder, EWCE_PLUGIN_MODULES)
    sources/               config models: Source, FormatProfile, Feed, ExtractionRule,
                           CustomFieldDefinition, KeywordSet, Keyword, FeedKeywordSet;
                           layered settings schema + effective-config resolver; clone;
                           import/export; seed data
    acquisition/           fetcher plugins (web_page, listing_to_documents, rss_atom, email,
                           manual_upload, custom_plugin) + polite HTTP client
    extraction/            parsers (html, pdf, docx, xlsx/xls/csv, txt, rtf, image, eml, zip),
                           rule-type plugins, custom-field extractors, date parser, keyword matcher
    collection/            FetchRun(+events), RawArtifact, CollectedMessage; pipeline service,
                           archive, dedupe/versioning, health, scheduler worker
    mailbox/               MailboxAccount, MailboxRule, MailboxSeenMessage; IMAP protocol plugin,
                           routing engine, post-processing
    ui/                    Bootstrap 5 operator/admin UI (views, templates)
  custom_plugins/          drop-in Python plugins (no core changes)
  data/artifacts/          immutable raw archive  ab/cd/<sha256>.<ext>
  logs/                    ewce.log (structured, rotating)
  tests/
```

## 3. Data model (summary)

```
Source 1─* Feed *─1 FormatProfile (live settings layer; rules/custom fields copied on create)
Feed 1─* ExtractionRule (ordered)      Feed 1─* CustomFieldDefinition (ordered)
Feed *─* KeywordSet (FeedKeywordSet)   KeywordSet 1─* Keyword
MailboxAccount 1─* MailboxRule ─> Feed        MailboxAccount 1─* MailboxSeenMessage (UID/Message-ID)
FetchRun (feed | mailbox) 1─* FetchRunEvent
RawArtifact (content-addressed by SHA-256; parent = .eml / .zip for attachments)
CollectedMessage ─> Source, Feed, Mailbox, RawArtifact, FetchRun, ExtractionRule,
                    supersedes (self), duplicate_of (self)
ConfigVersion (generic FK + JSON snapshot)   AuditLog
```

All config models inherit `TrackedModel` (created_at/updated_at/created_by/updated_by) and are
snapshotted to `ConfigVersion` on every save (rollback = apply snapshot, recorded as new version).

`CollectedMessage` reserves `processing_state` (default `pending`) and `processing_meta` (JSON) for
Scope 2; nothing in Scope 1 reads them.

## 4. Configuration layering

`settings` JSON on Source, FormatProfile, Feed and ExtractionRule (`settings_override`) all follow
one JSON Schema (`apps/sources/settings_schema.py`) with sections:
`fetch`, `files`, `scope`, `capture`, `dedupe`, `content`, `mapping`.

```
built-in defaults -> Source (+ its default language/timezone/headers/UA) -> FormatProfile -> Feed -> Rule
```
Dicts merge recursively; scalars and lists replace; an absent key means "inherit".
`effective_settings(feed, rule)` returns the merged dict plus a provenance map (which layer set
each value), shown in the UI "Effective configuration" viewer.

## 5. Plugin & schema-form mechanism

```python
@register
class ListingItemsRule(ExtractorPlugin):
    name = "listing_items"; label = "Listing item extraction"
    config_schema = {"type": "object", "properties": {
        "item_selector": {"type": "string", "format": "css-selector", "title": "Item selector"},
        "fields": {"type": "object", "additionalProperties": {"type": "string"}, "x-widget": "keyvalue"},
        ...}}
    def apply(self, items, ctx): ...
```
Kinds: `fetcher`, `parser`, `extractor` (rule types), `field_extractor` (custom fields),
`mailbox_protocol`. The UI builds a validated Django form from `config_schema`
(string/enum/integer/number/boolean/list/key-value/nested object, `format: regex|css-selector|xpath|env-var`
with live validation and a **Test** button), with an "Advanced: edit raw JSON" toggle. Validation
uses `jsonschema` + custom format checkers and reports human-readable, field-anchored errors.
Custom plugins: any `*.py` in `custom_plugins/` (or modules in `EWCE_PLUGIN_MODULES`) that
subclass a plugin base and use `@register` appear automatically with their forms.

## 6. Pipeline

```
Acquire (fetcher) -> Archive raw (SHA-256, immutable) -> Parse (by file type) ->
Rules in order (items -> items) -> Custom fields -> Normalize (NFC, digits, language,
legacy-font check) -> Keyword filter -> Dedupe/version -> Save -> FetchRun events/log
```
Per-feed DB lock (`lock_owner/lock_expires_at`, atomic UPDATE) so runs never overlap, even across
worker and web processes. Every failure becomes a FetchRun error; with an artifact it also becomes
an `extraction_failed` message.

Dedupe: key = configurable (`url`, `title`, `selector`, `date`, `regex`, `url+title`).
same key + same hash -> sighting only (seen_count++); same key + new hash -> new version
(`updated`, supersedes old); new key but same hash as another message in the feed -> `duplicate`.

## 7. Scheduler

`python manage.py run_worker` — APScheduler `BlockingScheduler` with three DB-driven jobs:
dispatch due feeds (interval/cron via `next_run_at`, active hours, thread pool), poll due
mailboxes, health check (silent / degraded / failing). No Redis/Celery.

## 8. Assumptions

1. One deployment, a handful of operators; volumes of thousands (not millions) of messages/month,
   so `LIKE` search on a normalized `search_text` column is sufficient (FULLTEXT can be added later).
2. `keyword_match_or_tag` = keep keyword matches as normal; keep non-matches too but tag them with
   the `no_keyword_match` quality flag (instead of `filtered_out`) so reviewers can see them.
3. Profiles are *live* for settings (editing a profile changes inheriting feeds) but rules and
   custom fields are *copied* into the feed when created, so feeds can diverge safely.
4. Credentials are stored only as env-var names; values live in `.env`.
5. Bangla keyword default match mode is `prefix` (Bangla inflects with suffixes: বন্যা/বন্যার/বন্যায়).
6. `.doc` (binary Word) has no pure-Python parser: stored raw and flagged `needs_attention`
   unless converted; `.docx` is fully supported.
7. Bootstrap 5 and Bootstrap Icons are vendored in `static/vendor` so the UI works offline.
8. Written permission or an agency email feed is the preferred long-term acquisition method;
   scraping is a fallback and respects robots.txt by default.

## 9. Decisions made during the build (M1–M5)

* **TLS:** several gov.bd sites omit their intermediate certificate. Instead of disabling verification,
  `truststore` makes Python verify against the Windows certificate store (`EWCE_USE_SYSTEM_CERTS`).
* **MariaDB 10.4:** besides the version gate, Django 5.x assumes `INSERT … RETURNING` (10.5+); the compat
  backend restores Django 4.2's feature check. `max_allowed_packet` is checked at run time and rows are
  truncated (flag `truncated`) before they could exceed it.
* **Keyword matching** is Bangla-aware: word boundaries include the whole Bangla block (vowel signs, virama),
  terms are NFC-normalized (য়/ড়/ঢ় have two encodings), and Bangla terms default to prefix matching.
* **Manual uploads** ignore the feed's allowed-types list (the operator chose the file; the run log says so);
  automated acquisition always honours it, and listing feeds never download disallowed types.
* **Health "layout change"** compares a run with the median of the previous successful runs: links found
  (listing feeds) or items extracted, and average text size per item.
* **Mailboxes** sit behind a small connection interface (`apps/mailbox/protocols.py`); routing counts only
  the conditions an admin filled in; email-level counters (`emails_routed`, `emails_unrouted`) are kept
  separate from message-status counters on the run.
* **Tests on history** reuse the real pipeline in dry-run mode, so "unchanged / updated / new" means
  exactly what a real run would do.
