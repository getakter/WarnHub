"""Generate validated Django forms from a plugin's JSON Schema.

Supported schema shapes -> widgets
  string (+format regex/css-selector/xpath/env-var)  text input (with Test button for selectors/regex)
  string + x-widget: textarea                        textarea
  string/integer + enum                              select
  integer / number                                   number input
  boolean                                            checkbox (tri-state Inherit/Yes/No in inherit mode)
  array of enum                                      checkbox list
  array of string                                    textarea, one value per line
  object + additionalProperties (no properties)      key = value lines
  object with properties                             nested group (flattened as parent__child)
  anything else / x-widget: json                     JSON textarea
Every form also has an "Advanced: edit raw JSON" toggle.

In *inherit* mode (layered settings), a blank field means "inherit from the layer below" and
the inherited value is shown as placeholder.
"""
import copy
import json

from django import forms

from apps.core.merge import get_path, prune

from .validation import validate

SEP = "__"
TESTABLE_FORMATS = {"regex", "css-selector", "xpath"}


def _type(schema):
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return t


def _strip_required(schema):
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("required", None)
            node.pop("minItems", None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def _set_path(data, path, value):
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _display(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(map(str, value)) if value else "(empty list)"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class SchemaForm(forms.Form):
    def __init__(self, schema, config=None, *, inherit=False, effective=None, **kwargs):
        super().__init__(**kwargs)
        self.schema = schema
        self.inherit = inherit
        self.effective = effective or {}
        self.initial_config = config or {}
        self.specs = {}
        self.groups = []
        self.config = None
        self._build(schema, (), None, set(schema.get("required", [])))
        self.fields["_use_raw"] = forms.BooleanField(
            required=False,
            label="Advanced: edit raw JSON",
            widget=forms.CheckboxInput(attrs={"class": "form-check-input schema-raw-toggle"}),
        )
        self.fields["_raw_json"] = forms.CharField(
            required=False,
            initial=json.dumps(self.initial_config, indent=2, ensure_ascii=False),
            widget=forms.Textarea(attrs={"rows": 16, "class": "form-control font-monospace schema-raw-json"}),
        )

    # -- building -------------------------------------------------------------------
    def _group(self, title, description=""):
        group = {"title": title, "description": description, "names": []}
        self.groups.append(group)
        return group

    def _build(self, schema, path, group, required):
        for key, sub in schema.get("properties", {}).items():
            sub_path = path + (key,)
            if _type(sub) == "object" and sub.get("properties") and sub.get("x-widget") != "json":
                if group is None:
                    child_group = self._group(sub.get("title", key.replace("_", " ").title()), sub.get("description", ""))
                else:
                    child_group = group
                self._build(sub, sub_path, child_group, set(sub.get("required", [])))
                continue
            target = group
            if target is None:
                target = next((g for g in self.groups if g["title"] == "General"), None) or self._group("General")
            name = SEP.join(sub_path)
            self.fields[name] = self._make_field(sub, sub_path, key in required and not self.inherit)
            target["names"].append(name)

    def full_clean(self):
        # In raw-JSON mode the structured fields are ignored, so they must not be "required".
        if self.is_bound and self.data.get(self.add_prefix("_use_raw")):
            for name in self.specs:
                self.fields[name].required = False
        super().full_clean()

    def _make_field(self, sub, path, required):
        kind, field = self._field_for(sub, required)
        self.specs["__".join(path)] = {"path": path, "kind": kind, "schema": sub}
        label = sub.get("title") or path[-1].replace("_", " ").capitalize()
        if len(path) > 2:
            label = f"{path[-2].replace('_', ' ').capitalize()}: {label}"
        field.label = label
        help_parts = [sub.get("description", "")]
        if "examples" in sub:
            help_parts.append("Example: " + "; ".join(_display(e) for e in sub["examples"]))
        field.help_text = " ".join(p for p in help_parts if p)
        current = get_path(self.initial_config, ".".join(path))
        if current is not None:
            field.initial = self._to_initial(kind, current)
        elif not self.inherit and "default" in sub:
            field.initial = self._to_initial(kind, sub["default"])
        inherited = get_path(self.effective, ".".join(path))
        if self.inherit and inherited is not None:
            field.widget.attrs["placeholder"] = f"inherit: {_display(inherited)}"[:200]
            field.inherited_display = _display(inherited)
        fmt = sub.get("format") or sub.get("x-widget")
        if fmt in TESTABLE_FORMATS or sub.get("x-widget") == "selector":
            field.widget.attrs["data-test-format"] = fmt if fmt in TESTABLE_FORMATS else "css-selector"
        return field

    def _field_for(self, sub, required):
        t = _type(sub)
        widget_hint = sub.get("x-widget")
        text_attrs = {"class": "form-control"}
        if "enum" in sub:
            labels = sub.get("x-enum-labels", {})
            choices = [(str(v), labels.get(str(v), str(v))) for v in sub["enum"]]
            if self.inherit or not required:
                choices = [("", "— inherit —" if self.inherit else "—")] + choices
            return "enum", forms.ChoiceField(
                choices=choices, required=required, widget=forms.Select(attrs={"class": "form-select"})
            )
        if t == "boolean":
            if self.inherit:
                return "tribool", forms.ChoiceField(
                    choices=[("", "— inherit —"), ("true", "Yes"), ("false", "No")],
                    required=False,
                    widget=forms.Select(attrs={"class": "form-select"}),
                )
            return "bool", forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))
        if t == "integer":
            return "int", forms.IntegerField(
                required=required, min_value=sub.get("minimum"), max_value=sub.get("maximum"),
                widget=forms.NumberInput(attrs=text_attrs),
            )
        if t == "number":
            return "float", forms.FloatField(
                required=required, min_value=sub.get("minimum"), max_value=sub.get("maximum"),
                widget=forms.NumberInput(attrs={**text_attrs, "step": "any"}),
            )
        if t == "array" and widget_hint != "json":
            items = sub.get("items", {})
            if "enum" in items:
                labels = items.get("x-enum-labels", {})
                return "multi", forms.MultipleChoiceField(
                    choices=[(v, labels.get(v, v)) for v in items["enum"]],
                    required=False,
                    widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
                )
            if _type(items) in {"string", "integer", "number"}:
                attrs = {"rows": 3, "class": "form-control font-monospace", "data-kind": "lines"}
                if items.get("format") in TESTABLE_FORMATS:
                    attrs["data-test-format"] = items["format"]
                return "lines", forms.CharField(required=False, widget=forms.Textarea(attrs=attrs))
        if t == "object" and widget_hint != "json" and not sub.get("properties"):
            return "keyvalue", forms.CharField(
                required=False,
                widget=forms.Textarea(attrs={"rows": 3, "class": "form-control font-monospace", "data-kind": "keyvalue"}),
            )
        if t == "string":
            if widget_hint == "textarea":
                return "str", forms.CharField(
                    required=required, widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"})
                )
            mono = sub.get("format") in TESTABLE_FORMATS | {"env-var"}
            attrs = {"class": "form-control" + (" font-monospace" if mono else "")}
            return "str", forms.CharField(required=required, widget=forms.TextInput(attrs=attrs), strip=False)
        return "json", forms.CharField(
            required=required, widget=forms.Textarea(attrs={"rows": 4, "class": "form-control font-monospace"})
        )

    @staticmethod
    def _to_initial(kind, value):
        if kind == "tribool":
            return "true" if value else "false"
        if kind == "enum":
            return str(value)
        if kind == "lines":
            return "\n".join(map(str, value or []))
        if kind == "keyvalue":
            return "\n".join(f"{k} = {v}" for k, v in (value or {}).items())
        if kind == "json":
            return json.dumps(value, indent=2, ensure_ascii=False)
        return value

    # -- cleaning -------------------------------------------------------------------
    def _from_field(self, name, spec, value):
        kind, sub = spec["kind"], spec["schema"]
        if kind == "tribool":
            return None if value in ("", None) else value == "true"
        if kind == "enum":
            if value in ("", None):
                return None
            if _type(sub) == "integer":
                return int(value)
            return value
        if kind == "str":
            return None if value in ("", None) else value
        if kind in {"lines", "multi"}:
            if kind == "lines":
                items = [line.strip() for line in (value or "").splitlines() if line.strip()]
                item_type = _type(sub.get("items", {}))
                try:
                    if item_type == "integer":
                        items = [int(i) for i in items]
                    elif item_type == "number":
                        items = [float(i) for i in items]
                except ValueError:
                    self.add_error(name, "Each line must be a number.")
                    return None
            else:
                items = list(value or [])
            if self.inherit and not items:
                return None
            return items
        if kind == "keyvalue":
            result = {}
            for line in (value or "").splitlines():
                if not line.strip():
                    continue
                if "=" not in line:
                    self.add_error(name, f"Line {line!r} must look like: key = value")
                    continue
                key, val = line.split("=", 1)
                result[key.strip()] = val.strip()
            return result or None
        if kind == "json":
            if value in ("", None):
                return None
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                self.add_error(name, f"Invalid JSON: {exc}")
                return None
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("_use_raw"):
            try:
                config = json.loads(cleaned.get("_raw_json") or "{}")
            except json.JSONDecodeError as exc:
                self.add_error("_raw_json", f"Invalid JSON: {exc}")
                return cleaned
            if not isinstance(config, dict):
                self.add_error("_raw_json", "The configuration must be a JSON object { ... }.")
                return cleaned
        else:
            config = {}
            for name, spec in self.specs.items():
                if name not in cleaned:
                    continue
                value = self._from_field(name, spec, cleaned[name])
                if value is not None:
                    _set_path(config, spec["path"], value)
            config = prune(config)
        schema = _strip_required(self.schema) if self.inherit else self.schema
        for issue in validate(schema, config):
            target = issue.field if issue.field in self.fields and not cleaned.get("_use_raw") else None
            if target is None and cleaned.get("_use_raw"):
                target = "_raw_json"
            self.add_error(target, str(issue) if target in (None, "_raw_json") else issue.message)
        self.config = config
        return cleaned

    # Templates cannot access names starting with "_"
    @property
    def raw_toggle(self):
        return self["_use_raw"]

    @property
    def raw_json(self):
        return self["_raw_json"]

    def grouped_fields(self):
        for group in self.groups:
            yield group, [self[name] for name in group["names"]]
