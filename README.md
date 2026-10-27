# WarnHub — early warning collection (Scope 1)

WarnHub collects official weather information, disaster alerts and advisories from any number of sources
(web pages, listing pages with PDF/Word/Excel links, RSS feeds, email inboxes and manual uploads) and
stores each item as a **Collected Message** next to its untouched original. Everything (sources,
feeds, selectors, keywords, file types, mailboxes and routing rules) is configured from the web UI,
without a developer.

> **Naming.** The product name comes from one setting, `EWCE_PRODUCT_NAME` in `.env` (default
> `WarnHub`); change it there to rename the UI. Internal identifiers keep the original `ewce` name and
> are not affected by renames: the `EWCE_*` settings in `.env`, the `ewce` Python package and database,
> `datartifacts`, and the `"format": "ewce-config"` id inside exported files.

Scope 1 is collection only. Sanitizing, classification, drafting, approval and dissemination
(Scope 2) are not built; `CollectedMessage.processing_state` / `processing_meta` and the service layer
are the extension points. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Contents:** [Setup](#1-windows-setup-xampp) · [Roles](#2-users-and-roles) · [Daily use](#3-daily-use-operators) ·
[Guides](#4-step-by-step-guides-admins) · [How it works](#5-how-collection-works) · [Optional components](#6-optional-components) ·
[Troubleshooting](#7-troubleshooting) · [Developers](#8-for-developers)

---

## 1. Windows setup (XAMPP)

Tested on Windows 11 with **Python 3.13**, **XAMPP MariaDB 10.4**, **Django 5.2 LTS**.

### 1.1 Prepare MariaDB (once)

1. In the XAMPP Control Panel start **MySQL**.
2. Raise the packet limit (long bulletins exceed XAMPP's 1 MB default). Open
   `C:\xampp\mysql\bin\my.ini`, find the section that starts with `[mysqld]` and set:
   ```ini
   max_allowed_packet=64M
   ```
   Save, then **Stop** and **Start** MySQL. (WarnHub still works without this, but long texts are
   truncated and flagged `truncated`; the dashboard shows a warning until it is fixed.)
3. Create the database (XAMPP **Shell** button, or phpMyAdmin → SQL):
   ```
   C:\xampp\mysql\bin\mysql.exe -u root -e "CREATE DATABASE ewce CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
   ```

### 1.2 Install WarnHub (PowerShell, in the project folder)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env          # set DJANGO_SECRET_KEY to a long random string; DB_PASSWORD if root has one
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py bootstrap            # roles + example data (safe to re-run)
.\.venv\Scripts\python manage.py createsuperuser
```

### 1.3 Run it (two terminals)

```powershell
# Terminal 1 — the web app (http://127.0.0.1:8000)
.\.venv\Scripts\python manage.py runserver

# Terminal 2 — the background worker: scheduled feeds, mailbox polling, health checks
.\.venv\Scripts\python manage.py run_worker
```
Stop either with **Ctrl+C**. Without the worker, nothing is collected on schedule (the dashboard
says so); you can still use **Run now**, **Poll now** and uploads.

**Run the worker without a terminal:** in Windows *Task Scheduler* create a task that runs every
10 minutes:
`C:\path\to\Songket\.venv\Scripts\python.exe C:\path\to\Songket\manage.py run_worker --once`
(start in `C:\path\to\Songket`). `--once` runs everything that is due, then exits.

### 1.4 MariaDB 10.4 note
Django 5.2 officially needs MariaDB 10.5+. The default `DB_ENGINE=ewce.db.mariadb_compat` restores
Django's 10.4 behaviour (one version gate and `INSERT … RETURNING`); the whole test suite runs on
10.4. After upgrading MariaDB set `DB_ENGINE=django.db.backends.mysql` in `.env`.

### 1.5 Updating
```powershell
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate
```
then restart the web app and the worker.

### 1.6 Backups
Back up the `ewce` database (phpMyAdmin → Export, or `mysqldump -u root ewce > ewce.sql`) **and** the
`data\` folder (`data\artifacts` holds every original file). Configuration can also be exported as
JSON (Configure → Import / export).

---

## 2. Users and roles

| Role | Can |
|---|---|
| **WarnHub Admin** (and every superuser) | configure everything: sources, feeds, rules, keyword sets, mailboxes, import/export, rollback |
| **WarnHub Operator** | view everything, Run now / Test feed / Poll now, upload or paste, mark messages *needs attention* |

Create people in **Django admin → Users** (top-right button, superusers only) and add them to the
group *WarnHub Admin* or *WarnHub Operator*.

---

## 3. Daily use (operators)

* **Dashboard** — red banner for feeds that are *failing*, *silent* or *degraded*, with the reason;
  health per source and feed; worker status; errors of the last 24 h; unrouted emails.
* **Messages** — search (Bangla or English; Bangla digits match ASCII digits), filter by source, feed,
  status, language, dates, matched keyword, quality flag or extra field; **Export CSV** (opens in
  Excel with Bangla intact).
* **Message page** — extracted text beside the original (web snapshot, PDF, image or download),
  matched keywords, extra fields, versions, the rule that extracted it; **Mark needs attention**.
* **Upload** — files (PDF, Word, Excel, CSV, text, RTF, images, .eml, ZIP) or pasted text into a feed.
* **Runs** — every collection run with counts and a step-by-step log.

### Message statuses
| Status | Meaning |
|---|---|
| `new` | first time this item was seen |
| `updated` | same item (same key) with changed content; links to the version it supersedes |
| `duplicate` | identical content already collected under another key/URL |
| `filtered_out` | saved, but excluded by the feed's keyword/title filters (reason shown) |
| `needs_attention` | collected but a human should look (legacy font, empty text, scanned without OCR, missing required field, …) |
| `extraction_failed` | the original is archived but could not be read (reason shown) |
| `unrouted` | an email that no mailbox rule matched |

Re-running a feed never duplicates: an unchanged item only increases its *seen* count.

---

## 4. Step-by-step guides (admins)

> Golden rule: **Inspect, configure, Test, then enable.** Every rule and field has a *Test* panel
> that uses the values in the form *before* you save.

### 4.1 Add a new source
1. **Configure → Sources → New source.**
2. Name, short **code** (e.g. `BWDB`), website, default language and timezone.
3. Record permission/terms in *Permission / terms* (e.g. "Written approval received 2026-01-15").
4. Optional *Default settings*: anything set here (timeouts, SSL, allowed file types, …) applies to
   all feeds of this source unless a feed overrides it.

### 4.2 Add a feed (a section of a source)
1. **Configure → Feeds → New feed**, choose the source, a name and a **format profile**:

   | Profile | Use for |
   |---|---|
   | Gov website listing with PDF/Word links | a notices page whose links are bulletins (each document → one message) |
   | Single-page bulletin | one page updated in place (changes become versions) |
   | Notice listing (one message per row) | a table/list where each row *is* the notice |
   | Bulletin split into sections | one page with headed sections (e.g. per division) |
   | RSS / Atom feed | standard feeds |
   | Email with PDF attachment | bulletins arriving by email (use with a mailbox rule) |
   | Manual upload / paste | operators upload files |

   The profile's rules are **copied** into the feed (change them freely); its settings stay
   **linked** as defaults.
2. **Edit** the feed:
   * **Entry URLs** — one per line; `{YYYY} {MM} {DD} {YY} {M} {D}` are replaced with today's date
     (Bangladesh time), e.g. `https://example.gov.bd/bulletin/{YYYY}-{MM}-{DD}.pdf`.
   * **Schedule** — *every N minutes* or a cron expression (`0 */3 * * *` = every 3 hours), optional
     active hours, **expected frequency** (the feed turns *silent* if nothing new arrives for that
     long), max items per run, lookback window.
   * **Settings** — blank = inherit (the inherited value is shown under each field):
     *Fetching* (User-Agent, headers, cookies, basic-auth **env-var names**, timeout, retries, SSL,
     JavaScript, robots.txt, delay), *Allowed file types* (+ min/max size and what to do with
     others: ignore / store raw only / flag), *URL scope* (include/exclude patterns, same domain,
     crawl depth, link selector, link-text filters, pagination), *Capture* (keyword mode, minimum
     text length, ignore-title patterns), *Duplicates & versions* (how the item key is built,
     ignore volatile text when comparing), *Language & encoding*, *Field mapping*.
3. Check the **Effective configuration** tab: every value and which layer set it
   (built-in → source → profile → feed → rule).
4. **Test feed** (dry run, nothing saved) → fix → **Enable**.

### 4.3 Find selectors with Inspect URL
1. **Inspect URL**, paste the page address (choose the feed to use its fetch settings and scope).
2. You see status, content type, title, all links (documents highlighted; ✓ = inside the feed's URL
   scope), suggested selectors for repeating rows, the page and its extracted text.
3. **Click any element** in the page preview to get a suggested CSS selector, or type one in the
   **Selector playground** (CSS, XPath or regex) — matches are highlighted in red.

### 4.4 Add or change extraction rules
Feed page → **Extraction rules** → *Add rule*. Rules run top to bottom (reorder with the arrows).

| Rule type | Does |
|---|---|
| HTML selector | keep the main content (CSS or XPath), remove clutter (nav, footer, ads), or take a value/attribute/link into any field |
| Listing items | each repeating row/card becomes a message; sub-selectors per field, e.g. `title = a`, `link = a@href`, `date = td:nth-child(3)` |
| Section / heading splitting | split by headings, bold lines or a regex; optionally keep only sections whose heading contains given words |
| Regex | named groups become fields, e.g. `Signal No\.?\s*(?P<signal>\d+)`; or one message per match |
| Date | published date from text, a selector, a regex, metadata or the file name; Bangla months/digits, সকাল/বিকাল times; formats are editable |
| PDF options | pages (`1-3,5`, `-2` = last two), text layer / OCR, tables, header/footer removal |
| Office options | Word paragraphs/tables; Excel sheets, row/column ranges, header row, one message per row |
| Email options | plain or HTML body, strip quoted replies and signatures (Bangla sign-offs included) |
| Custom Python function | for sites configuration cannot handle (see 4.10) |

*On no match* decides what happens when a rule finds nothing: continue, flag *needs attention*, or
mark *extraction_failed*.

**Test before saving:** *Test on URL* (or a file) shows highlighted matches and the exact messages
that would be produced; **Test on history** re-extracts the last N stored originals and shows
which messages would stay *unchanged* and which would change. Every save is versioned: **History →
Roll back to this** undoes a bad change.

### 4.5 Add a custom field (e.g. bulletin number, issue time, region, station)
Feed page → **Custom fields** → *Add custom field*: a name (`bulletin_number`), an extractor
(CSS, XPath, regex, *text after a label* such as `বুলেটিন নং` / `Issue time`, metadata, constant) and
optionally *required* (missing → *needs attention*). Test it, save. Values appear on messages, in
search/filters (*Extra field … contains*) and in the CSV export.

### 4.6 Keyword sets
**Configure → Keyword sets**: terms with variants/synonyms, language, kind (*include* / *exclude* /
*required*), match (whole word / **prefix — recommended for Bangla**, so বন্যা matches বন্যার and
বন্যায় / anywhere / regex), tag and weight. Bulk import/export as CSV (UTF-8; variants separated by
`|`). Assign sets on the feed page (*Keywords* tab) and choose the capture mode in the feed's
settings: *capture all* (just record matches), *only keyword matches* (others saved as
`filtered_out`), or *keep all and tag non-matches* (`no_keyword_match`).

### 4.7 Connect a mailbox and route emails
1. Put the password (for Gmail/Outlook an **app password**) in `.env`, e.g.
   `EWCE_MAILBOX_ALERTS_PASSWORD=abcd efgh ijkl mnop`, and restart the web app and worker.
2. **Configure → Mailboxes → New mailbox**: host, port, security (SSL 993 / STARTTLS 143), username,
   the **variable name** `EWCE_MAILBOX_ALERTS_PASSWORD`, folders to watch, poll interval, max emails
   per run, first-poll lookback, what to do after collecting (leave / mark read / move / label) and
   with unmatched emails.
3. **Test connection** — shows the folders and message counts.
4. **Add rule** (checked top to bottom): conditions on sender address or `@domain`, recipient,
   subject phrases (Bangla ok) or regex, body keywords, has attachment, attachment types; then the
   **target feed**, what to take (body / attachments / both), allowed attachment types and size,
   ignore inline images, *one message per email* (attachments' text appended) or *per attachment*,
   and an optional post-action.
5. **Inbox preview** lists recent emails (read-only) and the rule each would match. The
   **Rule tester** takes an .eml file / pasted source / a collected email and shows every rule's
   verdict condition by condition, plus the messages it would create.
6. **Poll now** to collect once, then **Enable** the mailbox; the worker polls it on schedule.

Every email's original `.eml` is archived; attachments are linked to it. Emails no rule matches are
saved as `unrouted` (never dropped) — find them under Messages → status *unrouted*. Emails are never
processed twice (tracked by folder UID and Message-ID).

### 4.8 Manual upload
**Upload**: choose the feed, add files and/or paste text, optional title, source URL and date
(Bangladesh time). The feed's rules, keywords and dedupe apply; the run log shows the result.

### 4.9 Export, import, clone, roll back
* **Configure → Import / export**: export everything, or use *Export* on a source, feed, profile,
  keyword set or mailbox page (a feed export includes what it depends on). Import with *skip*,
  *update* or *copy* for existing objects; tick **Dry run** first. Imports are all-or-nothing.
* **Clone** is on every source, feed, profile, rule, keyword set and mailbox (clones start disabled).
* **History** on every configuration object: before/after diff of each change and *Roll back*.

### 4.10 Custom Python plugin (when configuration is not enough)
Copy `custom_plugins/example_plugin.py`, rename the classes and `name`s, restart the web app and
worker. The new rule type / field extractor / fetcher appears in the UI with a form generated from its
`config_schema`; load errors are shown on the dashboard. A *Custom Python function* rule or fetcher
can call `ewce_custom_plugins.<file>:<function>`.

---

## 5. How collection works

```
Acquire → Archive original → Parse → Rules (in order) → Custom fields → Normalize → Keywords → Dedupe/version → Save → Log
```
* **Polite**: identifiable User-Agent (`EWCE_USER_AGENT`), timeouts, retries with backoff, a delay
  between requests to the same site, robots.txt respected (switch off per feed only with
  permission), download size limit. HTTPS is verified against the Windows certificate store
  (`EWCE_USE_SYSTEM_CERTS`), which fixes gov.bd sites that omit their intermediate certificate.
* **Originals** are stored once per content (SHA-256) under `data\artifacts`, read-only.
* **Bangla**: text is NFC-normalized; Bangla digits are searchable as ASCII; documents typed in
  legacy Bijoy/SutonnyMJ fonts are detected (font names and glyph patterns) and flagged
  `legacy_bangla_font` + *needs attention*; their garbled text is kept separately, never as clean text.
* **Never silent**: every skip and error is logged on the run; unreadable originals become
  `extraction_failed` messages; filtered items are kept as `filtered_out`.
* **Health**: *failing* (3 failed runs in a row), *silent* (nothing new for longer than expected),
  *degraded* (last run had errors, most documents failed, or the **layout-change check**: a sudden
  empty extraction or text size very different from recent runs).

Preferred long-term method: **written permission or an agency email feed** — more reliable than
scraping and unaffected by website redesigns.

---

## 6. Optional components

**OCR (scanned PDFs and images, Bangla + English)**
1. Install Tesseract for Windows (UB Mannheim build) and tick the *Bengali* language data.
2. In `.env`: `EWCE_ENABLE_OCR=True` and
   `EWCE_TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`; restart.
3. Configure → Plugins shows the OCR status. PDF rules choose *text layer*, *text layer then OCR*
   (default) or *always OCR*. Without OCR, scanned pages are flagged `ocr_unavailable`.

**JavaScript pages (Playwright)**
```powershell
.\.venv\Scripts\pip install -r requirements-optional.txt
.\.venv\Scripts\playwright install chromium
```
Set `EWCE_ENABLE_PLAYWRIGHT=True`, restart, and tick *Requires JavaScript* in the feed's fetch
settings (or *Render JS* in Inspect URL). Without it such feeds are flagged `js_not_rendered`.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard: *max_allowed_packet is 1024 KB* | §1.1 step 2, restart MySQL |
| `Can't connect to MySQL server` | start MySQL in XAMPP; check `DB_*` in `.env` |
| Bangla shows as `????` | database not utf8mb4: recreate it with the command in §1.1 |
| Dashboard: *No background worker is running* | start `run_worker` (§1.3) |
| Feed *failing*, `SSL error … certificate verify failed` | keep `EWCE_USE_SYSTEM_CERTS=True`; if it persists, untick *Verify SSL certificates* for that feed only |
| `robots.txt disallows …` | the site asks robots not to fetch that path: ask for permission/an email feed, or untick *Respect robots.txt* if you have permission |
| `HTTP 403` / `429` | slow down (raise *delay between requests*), set a User-Agent with contact details |
| Feed *degraded: possible layout change* | the site changed: Inspect URL, fix the selector, *Test on history*, save |
| Page has no links / empty text | it is built by JavaScript (§6) or the content is in an iframe: inspect the iframe's URL |
| Messages flagged `legacy_bangla_font` | the source used a Bijoy/SutonnyMJ font: ask for Unicode, or convert and upload |
| `ocr_unavailable` | install Tesseract (§6) or ask the agency for text PDFs |
| Mailbox: *Environment variable … is not set* | add it to `.env`, restart web app and worker |
| Mailbox login fails (Gmail/Outlook) | enable IMAP and use an **app password** |
| Everything arrives as `unrouted` | check rule conditions with the **Rule tester** |
| Run *skipped: already in progress* | another run holds the feed lock; stale locks expire after 30 minutes |
| A custom plugin is missing | dashboard/Plugins page shows its load error |

Logs: `logs\ewce.log` (rotating) and each run's log under **Runs**.

---

## 8. For developers

```powershell
.\.venv\Scripts\python -m pytest              # 166 tests on MariaDB (creates test_ewce)
.\.venv\Scripts\python -m pytest --create-db  # after model changes
```
Tests cover schema-driven validation and forms, selector/regex/section/listing rules, dates,
custom fields, mailbox routing and an end-to-end mailbox poll (in-memory protocol), the IMAP adapter,
dedupe/versioning, keyword filtering, Bangla normalization, legacy-font detection (text and PDF),
OCR fallback, layered config, export/import round trip, the worker/health logic and end-to-end feed
runs with mocked HTTP.

```
ewce/                 settings, urls, MariaDB 10.4 compat backend
apps/core/            tracked models, versions/rollback, audit, roles, Bangla utilities, layered merge
apps/plugins/         plugin registry, JSON-Schema validation, schema → form generator, plugin loader
apps/sources/         sources, feeds, profiles, rules, custom fields, keywords, layered settings, import/export, seed
apps/acquisition/     polite HTTP client, crawl helpers, fetcher plugins
apps/extraction/      file-type detection, parsers, rule & field plugins, dates, keywords, OCR, inspect helpers
apps/collection/      raw archive, pipeline, dedupe, normalize, locks, scheduling, health, worker, tests-on-history
apps/mailbox/         accounts, routing rules, seen-tracking, IMAP protocol, polling/routing service
apps/ui/              views and templates
custom_plugins/       drop-in plugins
```
Adding a mailbox protocol (POP3, Microsoft Graph, Gmail API): register a `MailboxProtocol` whose
`connect()` returns an object with the small interface documented in `apps/mailbox/protocols.py`.
