"""Schema-driven configuration: validation messages and auto-generated forms."""
import json

import pytest
from django.core.exceptions import ValidationError

from apps.plugins.registry import Plugin, Registry, registry
from apps.plugins.schema_forms import SchemaForm
from apps.plugins.validation import validate
from apps.sources.models import ExtractionRule, Feed, Source
from apps.sources.settings_schema import SETTINGS_SCHEMA

SCHEMA = {
    "type": "object",
    "required": ["item_selector"],
    "properties": {
        "item_selector": {"type": "string", "format": "css-selector"},
        "pattern": {"type": "string", "format": "regex"},
        "count": {"type": "integer", "minimum": 1, "default": 5},
        "enabled": {"type": "boolean", "default": True},
        "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
        "types": {"type": "array", "items": {"type": "string", "enum": ["pdf", "html"]}},
        "remove": {"type": "array", "items": {"type": "string"}},
        "fields": {"type": "object", "additionalProperties": {"type": "string"}},
        "nested": {"type": "object", "properties": {"x": {"type": "string"}}},
        "secret_env": {"type": "string", "format": "env-var"},
    },
}


def test_validation_messages_are_human_readable():
    issues = validate(SCHEMA, {"item_selector": "div[", "pattern": "(unclosed", "count": 0, "secret_env": "my pass"})
    text = " | ".join(map(str, issues))
    assert "not a valid CSS selector" in text
    assert "not a valid regular expression" in text
    assert "must be at least 1" in text
    assert "environment variable NAME" in text
    assert {i.field for i in issues} >= {"item_selector", "pattern", "count", "secret_env"}


def test_required_and_unknown_keys():
    issues = validate(SETTINGS_SCHEMA, {"fetch": {"timeout": 3}})
    assert any("Unknown setting" in str(i) for i in issues)
    assert any("is required" in str(i) for i in validate(SCHEMA, {}))


def test_form_generation_widgets():
    form = SchemaForm(SCHEMA, {"item_selector": "tr"})
    assert set(form.specs) >= {"item_selector", "count", "types", "remove", "fields", "nested__x"}
    assert form.specs["types"]["kind"] == "multi"
    assert form.specs["remove"]["kind"] == "lines"
    assert form.specs["fields"]["kind"] == "keyvalue"
    assert form.fields["item_selector"].widget.attrs["data-test-format"] == "css-selector"
    assert form.fields["count"].initial == 5  # schema default shown
    assert [g["title"] for g in form.groups] == ["General", "Nested"]


def test_form_round_trip_to_config():
    data = {
        "item_selector": "table tr", "pattern": r"(?P<n>\d+)", "count": "7", "enabled": "on", "mode": "b",
        "types": ["pdf"], "remove": "nav\nfooter\n\n", "fields": "title = a\nlink = a@href", "nested__x": "y",
    }
    form = SchemaForm(SCHEMA, data=data)
    assert form.is_valid(), form.errors
    assert form.config == {
        "item_selector": "table tr", "pattern": r"(?P<n>\d+)", "count": 7, "enabled": True, "mode": "b",
        "types": ["pdf"], "remove": ["nav", "footer"], "fields": {"title": "a", "link": "a@href"},
        "nested": {"x": "y"},
    }


def test_form_errors_anchor_to_fields():
    form = SchemaForm(SCHEMA, data={"item_selector": "div[[", "count": "2", "fields": "no equals sign"})
    assert not form.is_valid()
    assert "item_selector" in form.errors
    assert "fields" in form.errors


def test_raw_json_mode():
    form = SchemaForm(SCHEMA, data={"_use_raw": "on", "_raw_json": json.dumps({"item_selector": "li", "count": 3})})
    assert form.is_valid(), form.errors
    assert form.config == {"item_selector": "li", "count": 3}
    bad = SchemaForm(SCHEMA, data={"_use_raw": "on", "_raw_json": "{not json"})
    assert not bad.is_valid() and "_raw_json" in bad.errors
    invalid = SchemaForm(SCHEMA, data={"_use_raw": "on", "_raw_json": json.dumps({"count": 0})})
    assert not invalid.is_valid() and "_raw_json" in invalid.errors


def test_inherit_mode_blank_means_inherit():
    effective = {"fetch": {"timeout_seconds": 30, "verify_ssl": True}}
    form = SchemaForm(SETTINGS_SCHEMA, {}, inherit=True, effective=effective,
                      data={"fetch__timeout_seconds": "", "fetch__verify_ssl": "false", "files__allowed_types": []})
    assert form.is_valid(), form.errors
    assert form.config == {"fetch": {"verify_ssl": False}}
    assert "inherit: 30" in form.fields["fetch__timeout_seconds"].widget.attrs["placeholder"]


def test_new_plugin_class_gets_a_form_automatically():
    local = Registry()

    class MyFetcher(Plugin):
        kind = "fetcher"
        name = "my_fetcher"
        config_schema = {"type": "object", "properties": {"token_env": {"type": "string", "format": "env-var"}}}

    local.register(MyFetcher)
    plugin = local.get("fetcher", "my_fetcher")
    form = SchemaForm(plugin.schema(), data={"token_env": "MY_TOKEN"})
    assert form.is_valid() and form.config == {"token_env": "MY_TOKEN"}


def test_all_builtin_plugin_schemas_are_valid_and_render():
    from jsonschema import Draft202012Validator

    for kind in ("fetcher", "extractor", "field_extractor", "mailbox_protocol"):
        for name, cls in registry.all(kind).items():
            Draft202012Validator.check_schema(cls.schema())
            SchemaForm(cls.schema())  # must not raise
            assert not cls.validate_config(cls.default_config()) or cls.schema().get("required"), name


def test_custom_plugin_folder_is_loaded():
    assert registry.has("extractor", "example_strip_phrases")
    assert registry.has("field_extractor", "example_first_number_after")


@pytest.mark.django_db
def test_model_clean_rejects_bad_config():
    source = Source.objects.create(name="S", code="S")
    feed = Feed(source=source, name="F", slug="f", settings={"fetch": {"timeout_seconds": 0}}, is_active=True)
    with pytest.raises(ValidationError) as exc:
        feed.full_clean()
    text = " ".join(exc.value.messages)
    assert "timeout_seconds" in text and "entry URL" in text
    feed = Feed.objects.create(source=source, name="F", slug="f")
    rule = ExtractionRule(feed=feed, name="r", rule_type="listing_items", config={})
    with pytest.raises(ValidationError, match="item_selector"):
        rule.full_clean()
    rule = ExtractionRule(feed=feed, name="r", rule_type="nope")
    with pytest.raises(ValidationError, match="No Extraction rule type plugin named 'nope'"):
        rule.full_clean()
