"""JSON Schema validation with custom formats and human-readable, field-anchored errors."""
import re

from jsonschema import Draft202012Validator, FormatChecker

format_checker = FormatChecker()


@format_checker.checks("regex", raises=re.error)
def _check_regex(value):
    if isinstance(value, str) and value:
        re.compile(value)
    return True


@format_checker.checks("css-selector", raises=Exception)
def _check_css(value):
    if isinstance(value, str) and value.strip():
        import cssselect

        cssselect.GenericTranslator().css_to_xpath(value)
    return True


@format_checker.checks("xpath", raises=Exception)
def _check_xpath(value):
    if isinstance(value, str) and value.strip():
        from lxml import etree

        etree.XPath(value)
    return True


@format_checker.checks("env-var")
def _check_env_var(value):
    return not value or bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


FORMAT_MESSAGES = {
    "regex": "is not a valid regular expression",
    "css-selector": "is not a valid CSS selector",
    "xpath": "is not a valid XPath expression",
    "env-var": "must be an environment variable NAME (letters, digits, underscore), not a secret value",
}


class ValidationIssue:
    def __init__(self, path, message):
        self.path = path  # tuple of keys/indexes
        self.message = message

    @property
    def field(self):
        return "__".join(str(p) for p in self.path if not isinstance(p, int))

    def __str__(self):
        where = " → ".join(str(p) for p in self.path)
        return f"{where}: {self.message}" if where else self.message

    def __repr__(self):
        return f"ValidationIssue({self.path!r}, {self.message!r})"


def _humanize(error, schema):
    if error.validator == "format":
        fmt = error.validator_value
        detail = ""
        if error.cause is not None:
            detail = f" ({error.cause})"
        return f"{error.instance!r} {FORMAT_MESSAGES.get(fmt, 'has an invalid format')}{detail}"
    if error.validator == "required":
        return error.message.replace("is a required property", "is required")
    if error.validator == "enum":
        return f"{error.instance!r} is not one of: {', '.join(map(str, error.validator_value))}"
    if error.validator == "additionalProperties":
        return error.message.replace("Additional properties are not allowed", "Unknown setting(s)")
    if error.validator in {"minimum", "maximum"}:
        word = "at least" if error.validator == "minimum" else "at most"
        return f"must be {word} {error.validator_value} (got {error.instance!r})"
    if error.validator == "type":
        return f"must be of type {error.validator_value} (got {type(error.instance).__name__})"
    return error.message


def validate(schema, data):
    """Return a list of ValidationIssue (empty when valid)."""
    validator = Draft202012Validator(schema, format_checker=format_checker)
    issues = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.path))):
        issues.append(ValidationIssue(tuple(error.path), _humanize(error, schema)))
    return issues


def validate_or_raise(schema, data, label="configuration"):
    from django.core.exceptions import ValidationError

    issues = validate(schema, data)
    if issues:
        raise ValidationError([f"{label}: {issue}" for issue in issues])
