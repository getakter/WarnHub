"""Export / import configuration as JSON (to move between machines or share templates).

Objects are identified by natural keys, never database ids:
  source = code · profile = slug · keyword set = name · feed = "SOURCECODE/feed-slug" · mailbox = label
Runtime state (health, locks, timestamps) and collected data are never exported. Secrets are never in
the database, so exports only ever contain env-var *names*.
"""
import datetime as dt

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.audit import audit
from apps.core.versioning import record_version, version_context

from .models import CustomFieldDefinition, ExtractionRule, Feed, FeedKeywordSet, FormatProfile, Keyword, KeywordSet, Source
from .services import unique_value

FORMAT = "ewce-config"  # file-format id; kept stable so older exports still import
FORMAT_VERSION = 1

SOURCE_FIELDS = ["code", "name", "base_url", "description", "default_language", "default_timezone",
                 "default_user_agent", "default_headers", "contact_person", "permission_notes", "color", "settings",
                 "is_active", "is_example"]
PROFILE_FIELDS = ["slug", "name", "description", "method", "method_config", "settings", "rules_template",
                  "custom_fields_template", "is_active", "is_example"]
KEYWORD_SET_FIELDS = ["name", "description", "is_active", "is_example"]
FEED_FIELDS = ["slug", "name", "description", "is_active", "tags", "method", "method_config", "entry_urls", "settings",
               "schedule_type", "interval_minutes", "cron_expression", "active_hours_start", "active_hours_end",
               "expected_frequency_hours", "max_items_per_run", "lookback_days", "backfill_on_first_run"]
MAILBOX_FIELDS = ["label", "protocol", "host", "port", "security", "username", "password_env_var", "folders",
                  "poll_interval_minutes", "max_messages_per_run", "initial_lookback_days", "only_unseen",
                  "default_post_action", "default_post_target", "unmatched_post_action", "options", "is_active",
                  "is_example"]
MAILBOX_RULE_FIELDS = ["name", "enabled", "position", "match_mode", "from_patterns", "to_patterns", "subject_contains",
                       "subject_regex", "body_keywords", "has_attachment", "attachment_types", "take",
                       "allowed_attachment_types", "max_attachment_mb", "ignore_inline_images", "split", "post_action",
                       "post_target", "stop_processing"]


def feed_ref(feed):
    return f"{feed.source.code}/{feed.slug}"


def _values(obj, fields):
    out = {}
    for name in fields:
        value = getattr(obj, name)
        if isinstance(value, (dt.time, dt.date)):
            value = value.isoformat()
        out[name] = value
    return out


# --- Export --------------------------------------------------------------------------
def export_config(*, sources=(), profiles=(), keyword_sets=(), feeds=(), mailboxes=(), with_dependencies=True,
                  user=None, note=""):
    sources, profiles, keyword_sets = list(sources), list(profiles), list(keyword_sets)
    feeds, mailboxes = list(feeds), list(mailboxes)
    if with_dependencies:
        for mailbox in mailboxes:
            feeds += [r.target_feed for r in mailbox.rules.select_related("target_feed__source")]
        feeds = list({f.pk: f for f in feeds}.values())
        sources += [f.source for f in feeds]
        profiles += [f.profile for f in feeds if f.profile_id]
        keyword_sets += [k for f in feeds for k in f.keyword_sets.all()]
    unique = lambda objs: sorted({o.pk: o for o in objs}.values(), key=lambda o: o.pk)  # noqa: E731
    data = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "exported_at": timezone.now().isoformat(),
        "exported_by": getattr(user, "username", "") or "",
        "note": note,
        "sources": [_values(s, SOURCE_FIELDS) for s in unique(sources)],
        "profiles": [_values(p, PROFILE_FIELDS) for p in unique(profiles)],
        "keyword_sets": [
            {**_values(k, KEYWORD_SET_FIELDS), "keywords": [kw.as_dict() for kw in k.keywords.all()]}
            for k in unique(keyword_sets)
        ],
        "feeds": [
            {**_values(f, FEED_FIELDS), "source": f.source.code, "profile": f.profile.slug if f.profile_id else None,
             "keyword_sets": [link.keyword_set.name for link in FeedKeywordSet.objects.filter(feed=f).select_related("keyword_set")],
             "rules": [r.as_template() | {"position": r.position} for r in f.rules.all()],
             "custom_fields": [c.as_template() | {"position": c.position} for c in f.custom_fields.all()]}
            for f in unique(feeds)
        ],
        "mailboxes": [
            {**_values(m, MAILBOX_FIELDS),
             "rules": [{**_values(r, MAILBOX_RULE_FIELDS), "target_feed": feed_ref(r.target_feed)}
                       for r in m.rules.select_related("target_feed__source")]}
            for m in unique(mailboxes)
        ],
    }
    return data


def export_all(user=None):
    from apps.mailbox.models import MailboxAccount

    return export_config(sources=Source.objects.all(), profiles=FormatProfile.objects.all(),
                         keyword_sets=KeywordSet.objects.all(), feeds=Feed.objects.select_related("source"),
                         mailboxes=MailboxAccount.objects.all(), user=user, note="Full configuration")


# --- Import --------------------------------------------------------------------------
class ImportReport:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.actions = []  # (kind, key, action, detail)
        self.errors = []
        self.warnings = []
        self.applied = False

    def add(self, kind, key, action, detail=""):
        self.actions.append((kind, key, action, detail))

    @property
    def ok(self):
        return not self.errors

    def counts(self):
        out = {}
        for _kind, _key, action, _detail in self.actions:
            out[action] = out.get(action, 0) + 1
        return out


class _Importer:
    def __init__(self, data, on_conflict, report):
        self.data, self.on_conflict, self.report = data, on_conflict, report
        self.sources, self.profiles, self.keyword_sets, self.feeds = {}, {}, {}, {}

    # generic upsert ------------------------------------------------------------------
    def _save(self, kind, key, obj):
        try:
            obj.full_clean()
        except ValidationError as exc:
            self.report.errors.append(f"{kind} {key}: " + "; ".join(exc.messages))
            return False
        obj.save()
        return True

    def _upsert(self, kind, model, lookup, values, rename):
        """rename: {field: max_length} made unique in copy mode. Returns (obj, action) or (None, None)."""
        key = "/".join(v.code if isinstance(v, Source) else str(v) for v in lookup.values())
        existing = model.objects.filter(**lookup).first()
        if existing is not None and self.on_conflict == "skip":
            self.report.add(kind, key, "skip", "already exists")
            return existing, "skip"
        if existing is not None and self.on_conflict == "update":
            obj = existing
            action = "update"
        else:
            obj = model()
            action = "create"
        for name, value in values.items():
            setattr(obj, name, value)
        for name, value in lookup.items():
            setattr(obj, name, value)
        if existing is not None and self.on_conflict == "copy":
            scope = {k: v for k, v in lookup.items() if isinstance(v, Source)}
            for field, max_length in rename.items():
                base = getattr(obj, field)
                suffix = "-copy" if field in {"slug", "code"} else " (imported)"
                setattr(obj, field, unique_value(model, field, f"{base}{suffix}", max_length, **scope))
            action = "copy"
        if not self._save(kind, key, obj):
            return None, None
        self.report.add(kind, key, action, "" if action != "copy" else f"saved as {getattr(obj, list(rename)[0])}")
        return obj, action

    # sections ------------------------------------------------------------------------
    def sources_(self):
        for row in self.data.get("sources", []):
            values = {k: row[k] for k in SOURCE_FIELDS if k in row and k != "code"}
            obj, _ = self._upsert("source", Source, {"code": row["code"]}, values, {"code": 40, "name": 200})
            if obj:
                self.sources[row["code"]] = obj

    def profiles_(self):
        for row in self.data.get("profiles", []):
            values = {k: row[k] for k in PROFILE_FIELDS if k in row and k != "slug"}
            obj, _ = self._upsert("profile", FormatProfile, {"slug": row["slug"]}, values, {"slug": 100, "name": 200})
            if obj:
                self.profiles[row["slug"]] = obj

    def keyword_sets_(self):
        for row in self.data.get("keyword_sets", []):
            values = {k: row[k] for k in KEYWORD_SET_FIELDS if k in row and k != "name"}
            obj, action = self._upsert("keyword set", KeywordSet, {"name": row["name"]}, values, {"name": 200})
            if obj is None:
                continue
            self.keyword_sets[row["name"]] = obj
            if action in {"create", "update", "copy"}:
                obj.keywords.all().delete()
                keywords = [Keyword(keyword_set=obj, **Keyword.clean_dict(k)) for k in row.get("keywords", [])]
                for keyword in keywords:
                    keyword.normalize()
                Keyword.objects.bulk_create(keywords)
                record_version(obj, comment=f"Imported {len(keywords)} keywords")

    def _source_for(self, code):
        source = self.sources.get(code) or Source.objects.filter(code=code).first()
        if source is None:
            self.report.errors.append(f"Source '{code}' is neither in the file nor in this database")
        return source

    def feeds_(self):
        for row in self.data.get("feeds", []):
            ref = f"{row['source']}/{row['slug']}"
            source = self._source_for(row["source"])
            if source is None:
                continue
            values = {k: row[k] for k in FEED_FIELDS if k in row and k != "slug"}
            values["profile"] = None
            if row.get("profile"):
                values["profile"] = self.profiles.get(row["profile"]) or FormatProfile.objects.filter(slug=row["profile"]).first()
                if values["profile"] is None:
                    self.report.warnings.append(f"Feed {ref}: profile '{row['profile']}' not found; imported without it")
            obj, action = self._upsert("feed", Feed, {"source": source, "slug": row["slug"]}, values,
                                       {"slug": 100, "name": 200})
            if obj is None:
                continue
            obj.source = source
            self.feeds[ref] = obj
            if action == "skip":
                continue
            obj.rules.all().delete()
            obj.custom_fields.all().delete()
            for index, tpl in enumerate(row.get("rules", []), start=1):
                rule = ExtractionRule(feed=obj, position=tpl.get("position", index * 10), name=tpl.get("name") or tpl["rule_type"],
                                      enabled=tpl.get("enabled", True), rule_type=tpl["rule_type"],
                                      applies_to_types=tpl.get("applies_to_types", []),
                                      applies_to_url_pattern=tpl.get("applies_to_url_pattern", ""),
                                      config=tpl.get("config", {}), settings_override=tpl.get("settings_override", {}),
                                      on_no_match=tpl.get("on_no_match", "continue"), notes=tpl.get("notes", ""))
                self._save("rule", f"{ref} › {rule.name}", rule)
            for index, tpl in enumerate(row.get("custom_fields", []), start=1):
                field = CustomFieldDefinition(feed=obj, position=tpl.get("position", index * 10), name=tpl["name"],
                                              label=tpl.get("label", ""), extractor=tpl["extractor"],
                                              config=tpl.get("config", {}), enabled=tpl.get("enabled", True),
                                              applies_to_types=tpl.get("applies_to_types", []),
                                              required=tpl.get("required", False))
                self._save("custom field", f"{ref} › {field.name}", field)
            FeedKeywordSet.objects.filter(feed=obj).delete()
            for name in row.get("keyword_sets", []):
                kset = self.keyword_sets.get(name) or KeywordSet.objects.filter(name=name).first()
                if kset is None:
                    self.report.warnings.append(f"Feed {ref}: keyword set '{name}' not found; not linked")
                    continue
                FeedKeywordSet.objects.create(feed=obj, keyword_set=kset)

    def mailboxes_(self):
        from apps.mailbox.models import MailboxAccount, MailboxRule

        for row in self.data.get("mailboxes", []):
            values = {k: row[k] for k in MAILBOX_FIELDS if k in row and k != "label"}
            obj, action = self._upsert("mailbox", MailboxAccount, {"label": row["label"]}, values, {"label": 200})
            if obj is None or action == "skip":
                continue
            obj.rules.all().delete()
            for tpl in row.get("rules", []):
                feed = self.feeds.get(tpl["target_feed"])
                if feed is None:
                    code, _, slug = tpl["target_feed"].partition("/")
                    feed = Feed.objects.filter(source__code=code, slug=slug).first()
                if feed is None:
                    self.report.errors.append(f"Mailbox {row['label']}: rule '{tpl['name']}' targets unknown feed {tpl['target_feed']}")
                    continue
                rule = MailboxRule(mailbox=obj, target_feed=feed, **{k: tpl[k] for k in MAILBOX_RULE_FIELDS if k in tpl})
                self._save("mailbox rule", f"{row['label']} › {rule.name}", rule)

    def run(self):
        self.sources_()
        self.profiles_()
        self.keyword_sets_()
        self.feeds_()
        self.mailboxes_()


def validate_structure(data):
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return [f"This is not a {settings.EWCE_PRODUCT_NAME} configuration export (missing \"format\": \"{FORMAT}\")."]
    if data.get("version", 0) > FORMAT_VERSION:
        return [f"File format version {data['version']} is newer than this installation supports ({FORMAT_VERSION})."]
    errors = []
    for section, key in (("sources", "code"), ("profiles", "slug"), ("keyword_sets", "name"),
                         ("feeds", "slug"), ("mailboxes", "label")):
        rows = data.get(section, [])
        if not isinstance(rows, list):
            errors.append(f"'{section}' must be a list")
            continue
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict) or key not in row:
                errors.append(f"{section} #{index}: missing '{key}'")
            elif section == "feeds" and "source" not in row:
                errors.append(f"feeds #{index}: missing 'source'")
    return errors


def import_config(data, *, on_conflict="skip", dry_run=True, user=None):
    """Import everything or nothing. In dry-run mode (or on any error) all changes are rolled back."""
    report = ImportReport(dry_run)
    if on_conflict not in {"skip", "update", "copy"}:
        report.errors.append(f"Unknown conflict mode {on_conflict!r}")
        return report
    report.errors.extend(validate_structure(data))
    if report.errors:
        return report
    with transaction.atomic():
        with version_context(change_type="import", comment=f"Imported ({on_conflict})"):
            _Importer(data, on_conflict, report).run()
        if report.errors or dry_run:
            transaction.set_rollback(True)
        else:
            report.applied = True
    if report.applied:
        audit("import_config", None, user=user, on_conflict=on_conflict, counts=report.counts())
    return report
