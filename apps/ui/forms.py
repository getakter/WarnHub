import json

from django import forms

from apps.collection.models import CollectedMessage
from apps.mailbox.models import MailboxAccount, MailboxRule
from apps.plugins.registry import registry
from apps.sources.models import (
    CustomFieldDefinition, ExtractionRule, Feed, FormatProfile, Keyword, KeywordSet, Source,
)
from apps.sources.settings_schema import FILE_TYPE_LABELS, FILE_TYPES

FILE_TYPE_CHOICES = [(t, FILE_TYPE_LABELS[t]) for t in FILE_TYPES]


class LinesField(forms.CharField):
    """A list edited as one value per line."""

    widget = forms.Textarea

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({"rows": 3, "class": "form-control font-monospace"})

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return "\n".join(map(str, value))
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if isinstance(value, list):
            return value
        return [line.strip() for line in (value or "").splitlines() if line.strip()]


class KeyValueField(forms.CharField):
    widget = forms.Textarea

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({"rows": 3, "class": "form-control font-monospace"})

    def prepare_value(self, value):
        if isinstance(value, dict):
            return "\n".join(f"{k} = {v}" for k, v in value.items())
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if isinstance(value, dict):
            return value
        result = {}
        for line in (value or "").splitlines():
            if not line.strip():
                continue
            if "=" not in line:
                raise forms.ValidationError(f"Line {line!r} must look like: Name = value")
            key, val = line.split("=", 1)
            result[key.strip()] = val.strip()
        return result


class JsonField(forms.CharField):
    widget = forms.Textarea

    def __init__(self, *args, expect=None, **kwargs):
        self.expect = expect
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({"rows": 8, "class": "form-control font-monospace"})

    def prepare_value(self, value):
        if isinstance(value, (list, dict)):
            return json.dumps(value, indent=2, ensure_ascii=False)
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if isinstance(value, (list, dict)):
            return value
        if not (value or "").strip():
            return self.expect() if self.expect else None
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc
        if self.expect and not isinstance(data, self.expect):
            raise forms.ValidationError(f"Expected a JSON {self.expect.__name__}")
        return data


class BootstrapMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput,)):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.CheckboxSelectMultiple):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


class SourceForm(BootstrapMixin, forms.ModelForm):
    default_headers = KeyValueField(label="Default HTTP headers", help_text="Name = value, one per line")

    class Meta:
        model = Source
        fields = [
            "name", "code", "base_url", "description", "default_language", "default_timezone",
            "default_user_agent", "default_headers", "contact_person", "permission_notes", "logo", "color",
            "is_active",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "permission_notes": forms.Textarea(attrs={"rows": 2}),
            "color": forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
        }


class FeedCreateForm(BootstrapMixin, forms.Form):
    source = forms.ModelChoiceField(queryset=Source.objects.all())
    name = forms.CharField(max_length=200)
    profile = forms.ModelChoiceField(
        queryset=FormatProfile.objects.filter(is_active=True), required=False,
        help_text="Copies the profile's rules and custom fields; its settings stay linked as defaults.",
    )
    method = forms.ChoiceField(choices=[], required=False, help_text="Only used when no profile is chosen")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = registry.choices("fetcher")


class FeedForm(BootstrapMixin, forms.ModelForm):
    entry_urls = LinesField(help_text=Feed._meta.get_field("entry_urls").help_text)
    tags = LinesField(help_text="One tag per line")
    method = forms.ChoiceField(choices=[], label="Acquisition method")

    class Meta:
        model = Feed
        fields = [
            "name", "slug", "description", "is_active", "tags", "method", "profile", "entry_urls",
            "schedule_type", "interval_minutes", "cron_expression", "active_hours_start", "active_hours_end",
            "expected_frequency_hours", "max_items_per_run", "lookback_days", "backfill_on_first_run",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "active_hours_start": forms.TimeInput(attrs={"type": "time"}),
            "active_hours_end": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = registry.choices("fetcher")
        self.fields["method"].widget.attrs["data-reload-on-change"] = "method"


class ProfileForm(BootstrapMixin, forms.ModelForm):
    method = forms.ChoiceField(choices=[], label="Default acquisition method")
    rules_template = JsonField(expect=list, label="Rule templates (JSON list)",
                               help_text="Copied into feeds created from this profile.")
    custom_fields_template = JsonField(expect=list, label="Custom field templates (JSON list)")

    class Meta:
        model = FormatProfile
        fields = ["name", "slug", "description", "method", "rules_template", "custom_fields_template", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = registry.choices("fetcher")
        self.fields["method"].widget.attrs["data-reload-on-change"] = "method"
        self.fields["slug"].required = False


class RuleForm(BootstrapMixin, forms.ModelForm):
    applies_to_types = forms.MultipleChoiceField(
        choices=FILE_TYPE_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Leave all unticked to apply to every file type.",
    )

    class Meta:
        model = ExtractionRule
        fields = ["name", "enabled", "applies_to_types", "applies_to_url_pattern", "on_no_match", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class CustomFieldForm(BootstrapMixin, forms.ModelForm):
    applies_to_types = forms.MultipleChoiceField(
        choices=FILE_TYPE_CHOICES, required=False, widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = CustomFieldDefinition
        fields = ["name", "label", "enabled", "required", "applies_to_types"]


class KeywordSetForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = KeywordSet
        fields = ["name", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class KeywordForm(BootstrapMixin, forms.ModelForm):
    variants = LinesField(help_text="Synonyms / spelling variants, one per line")

    class Meta:
        model = Keyword
        fields = ["term", "language", "variants", "kind", "match_mode", "case_sensitive", "tag", "weight", "enabled"]


class KeywordCsvImportForm(BootstrapMixin, forms.Form):
    file = forms.FileField(help_text="CSV with header: term,language,variants,kind,match_mode,case_sensitive,tag,weight,enabled "
                                     "(variants separated by |). UTF-8.")
    replace = forms.BooleanField(required=False, label="Replace all existing keywords in this set")


class FeedKeywordSetsForm(BootstrapMixin, forms.Form):
    keyword_sets = forms.ModelMultipleChoiceField(
        queryset=KeywordSet.objects.all(), required=False, widget=forms.CheckboxSelectMultiple
    )


class MailboxForm(BootstrapMixin, forms.ModelForm):
    folders = LinesField(help_text="One folder per line (default INBOX)")
    protocol = forms.ChoiceField(choices=[])

    class Meta:
        model = MailboxAccount
        fields = [
            "label", "protocol", "host", "port", "security", "username", "password_env_var", "folders",
            "poll_interval_minutes", "max_messages_per_run", "initial_lookback_days", "only_unseen",
            "default_post_action", "default_post_target", "unmatched_post_action", "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["protocol"].choices = registry.choices("mailbox_protocol")


class MailboxRuleForm(BootstrapMixin, forms.ModelForm):
    from_patterns = LinesField(help_text="name@agency.gov.bd or @agency.gov.bd, one per line")
    to_patterns = LinesField(help_text="Recipient addresses or @domain")
    subject_contains = LinesField(help_text="Any of these phrases (Bangla ok), one per line")
    body_keywords = LinesField(help_text="Any of these words in the body")
    attachment_types = forms.MultipleChoiceField(choices=FILE_TYPE_CHOICES, required=False,
                                                 widget=forms.CheckboxSelectMultiple)
    allowed_attachment_types = forms.MultipleChoiceField(choices=FILE_TYPE_CHOICES, required=False,
                                                         widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_feed"].queryset = Feed.objects.select_related("source").order_by("source__code", "name")

    class Meta:
        model = MailboxRule
        fields = [
            "name", "enabled", "match_mode", "from_patterns", "to_patterns", "subject_contains", "subject_regex",
            "body_keywords", "has_attachment", "attachment_types", "target_feed", "take", "allowed_attachment_types",
            "max_attachment_mb", "ignore_inline_images", "split", "post_action", "post_target", "stop_processing",
        ]


class ImportConfigForm(BootstrapMixin, forms.Form):
    file = forms.FileField(required=False, help_text="A .json configuration export")
    text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 8, "class": "font-monospace"}),
                           help_text="…or paste JSON")
    on_conflict = forms.ChoiceField(
        choices=[("skip", "Skip objects that already exist"), ("update", "Update existing objects"),
                 ("copy", "Import as copies with new names")],
        initial="skip",
    )
    dry_run = forms.BooleanField(required=False, initial=True, label="Dry run (show what would happen)")


# --- M2: upload, message filters -----------------------------------------------------
class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


class UploadForm(BootstrapMixin, forms.Form):
    feed = forms.ModelChoiceField(queryset=Feed.objects.select_related("source"),
                                  help_text="The message is stored under this feed and uses its rules and keywords.")
    files = MultipleFileField(required=False, label="Files",
                              help_text="PDF, Word, Excel, CSV, text, RTF, images, .eml or ZIP. Several allowed.")
    text = forms.CharField(required=False, label="…or paste text", widget=forms.Textarea(attrs={"rows": 8, "class": "bn"}))
    title = forms.CharField(required=False, max_length=1000, help_text="Optional; used when one file or text is given")
    url = forms.URLField(required=False, max_length=2000, label="Source URL", help_text="Where this came from (optional)")
    published_at = forms.DateTimeField(required=False, label="Published at (Bangladesh time)",
                                       widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    language = forms.ChoiceField(required=False, choices=[("", "Feed default"), ("auto", "Auto-detect"),
                                                          ("bn", "Bangla"), ("en", "English"), ("mixed", "Mixed")])

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("files") and not (cleaned.get("text") or "").strip():
            raise forms.ValidationError("Choose at least one file or paste some text.")
        return cleaned


QUALITY_FLAG_CHOICES = [("", "Any flag")] + [(f, f) for f in CollectedMessage.QUALITY_FLAGS]


class MessageFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    source = forms.ModelChoiceField(queryset=Source.objects.all(), required=False)
    feed = forms.ModelChoiceField(queryset=Feed.objects.select_related("source"), required=False)
    status = forms.MultipleChoiceField(required=False, choices=[])
    language = forms.ChoiceField(required=False, choices=[("", "Any language"), ("bn", "Bangla"), ("en", "English"),
                                                          ("mixed", "Mixed"), ("unknown", "Unknown")])
    date_field = forms.ChoiceField(required=False, choices=[("collected_at", "Collected"), ("published_at", "Published")])
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    keyword = forms.CharField(required=False, help_text="Matched keyword term")
    flag = forms.ChoiceField(required=False, choices=QUALITY_FLAG_CHOICES)
    field_name = forms.SlugField(required=False, label="Extra field")
    field_value = forms.CharField(required=False, label="contains")
    latest_only = forms.BooleanField(required=False, label="Latest versions only")
    manual = forms.ChoiceField(required=False, choices=[("", "Collected & manual"), ("yes", "Manual only"),
                                                        ("no", "Collected only")])
    run = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = CollectedMessage.STATUSES
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.SelectMultiple):
                field.widget.attrs.update({"class": "form-select form-select-sm", "size": 4})
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select form-select-sm")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control form-control-sm")


class AttentionForm(forms.Form):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}))
