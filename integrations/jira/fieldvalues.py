"""Single-value Jira field types shared by Board and workflow transitions."""

import datetime
import math
import re

from . import adf

SCALAR_TYPES = {
    "string": "text", "url": "text", "number": "number", "float": "number",
    "integer": "number", "date": "date", "datetime": "datetime", "boolean": "boolean",
}
TEXTAREA = "com.atlassian.jira.plugin.system.customfieldtypes:textarea"


def scalar(value, spec):
    """Validate a scalar and normalize it for display and comparison."""
    kind = spec["kind"]
    if kind in {"text", "multiline"} and isinstance(value, str) and value.strip():
        return value
    if kind == "number" and not isinstance(value, bool):
        try:
            if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value)):
                return int(value)
            number = float(value)
            if math.isfinite(number) and (not spec.get("integer") or number.is_integer()):
                return int(number) if number.is_integer() else number
        except (TypeError, ValueError, OverflowError):
            pass
    if kind == "date" and isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    if kind == "datetime" and isinstance(value, str):
        try:
            iso = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", value.replace("Z", "+00:00"))
            parsed = datetime.datetime.fromisoformat(iso)
            if parsed.tzinfo is not None:
                return parsed.astimezone(datetime.timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    if kind == "boolean" and isinstance(value, bool):
        return value
    raise ValueError("Enter a valid %s for %s." % (kind, spec["name"]))


def wire_scalar(value, spec, flavour):
    value = scalar(value, spec)
    if spec["kind"] == "datetime":
        date = datetime.datetime.fromisoformat(value)
        return date.isoformat(timespec="milliseconds").replace("+00:00", "+0000")
    if spec["kind"] == "multiline":
        return adf.from_text(value, flavour)
    return value


def transition_input(field, metadata):
    schema = metadata.get("schema") or {}
    kind = SCALAR_TYPES.get(schema.get("type"))
    if field in {"description", "environment"} or schema.get("custom") == TEXTAREA:
        kind = "multiline"
    if kind:
        return {"kind": kind, "integer": schema.get("type") == "integer",
                "name": str(metadata.get("name") or field)}
    return None
