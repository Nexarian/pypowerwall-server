"""
Multi-Gateway Field Naming

When more than one gateway is configured, the legacy endpoints (/strings,
/freq, /alerts, /alerts/pw) return every gateway's data in one document and
need a per-gateway label in each key so they cannot collide. There is no
agreed layout for those keys, so it is configurable with Python format
strings (str.format mini-language: ``{name}``, ``{index:02d}``, ``{din:>10}``,
and for string values an integer spec slices: ``{din:-3}`` is the last three
characters, ``{din:3}`` the first three).

Tag (the per-gateway label):
    PW_GATEWAY_TAG              default "{name}"
    placeholders: {name} {id} {din} {suffix} {index} {host}
      {din} is the gateway id as configured in PW_GATEWAYS (the DIN);
      {suffix} == {din:-3}, the DIN suffix the old proxy used;
      {name} falls back to {suffix} when the gateway has no distinct name;
      {index} is the gateway's 1-based position in PW_GATEWAYS.
    Placeholder names are case-insensitive ({DIN:-3} works).
    A gateway can set "tag" in PW_GATEWAYS to bypass the template.

Field formats (where the tag goes in a key), placeholders {tag} {field}:
    PW_GATEWAY_FIELD_FORMAT          default "{tag}_{field}"; used by every
                                     category that has no format of its own
    PW_GATEWAY_SOLAR_STRING_FORMAT   /strings keys       ("A" -> "1JG_A")
    PW_GATEWAY_ALERT_FORMAT          /alerts, /alerts/pw ("1JG_IslandChecksFailed")
    PW_GATEWAY_FREQ_FORMAT           /freq ISLAND/METER/PVAC fields ("1JG_PVAC_Fout")

Rendered tags are sanitized to [A-Za-z0-9_-] (anything else becomes "_") so
they are safe as InfluxDB field keys and in Grafana queries. A field template
that does not contain {field}, or that fails to format, would produce
colliding or broken keys, so it is rejected (logged once) and the fallback
is used instead: the category's default, then PW_GATEWAY_FIELD_FORMAT, then
the built-in "{tag}_{field}".
"""
import logging
import re
import string
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TAG_FORMAT = "{name}"
DEFAULT_FIELD_FORMAT = "{tag}_{field}"

# Categories of tagged fields, in the order a user would meet them. The
# settings attribute is "gateway_<category>_format" (env PW_GATEWAY_<CATEGORY>_FORMAT).
CATEGORIES = ("solar_string", "alert", "freq")

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_SLICE_SPEC = re.compile(r"-?\d+")


class _Formatter(string.Formatter):
    """str.format with case-insensitive plain names, slicing specs and no attribute access."""

    def get_field(self, field_name, args, kwargs):
        key = field_name.lower()
        if key not in kwargs:
            raise KeyError(field_name)
        return kwargs[key], field_name

    def format_field(self, value, format_spec):
        if isinstance(value, str) and _SLICE_SPEC.fullmatch(format_spec or ""):
            n = int(format_spec)
            return value[:n] if n >= 0 else value[n:]
        return super().format_field(value, format_spec)


_formatter = _Formatter()
_warned: set = set()


def render(template: str, values: Dict[str, Any]) -> str:
    """Render ``template`` with str.format semantics (raises on a bad template)."""
    return _formatter.vformat(template, (), {k.lower(): v for k, v in values.items()})


def sanitize_tag(tag: str) -> str:
    return _UNSAFE.sub("_", tag).strip("_")


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def gateway_tag(gateway, index: int, tag_format: Optional[str] = None) -> str:
    """Per-gateway label for multi-gateway field names.

    An explicit ``gateway.tag`` wins; otherwise ``tag_format`` is rendered with
    the gateway's name, id/din, suffix, index and host. An empty or invalid
    result falls back to the DIN suffix so keys never lose their gateway label.
    """
    explicit = getattr(gateway, "tag", None)
    if explicit:
        tag = sanitize_tag(str(explicit))
        if tag:
            return tag
    din = gateway.id
    suffix = din[-3:]
    # A gateway without a distinct name reports name == id; use the suffix for
    # {name} then, so the default template never yields a full DIN.
    name = gateway.name if gateway.name and gateway.name != din else suffix
    template = tag_format or DEFAULT_TAG_FORMAT
    try:
        tag = sanitize_tag(
            render(
                template,
                {
                    "name": name,
                    "id": din,
                    "din": din,
                    "suffix": suffix,
                    "index": index,
                    "host": getattr(gateway, "host", None) or "",
                },
            )
        )
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        _warn_once(
            "tag:" + template,
            "PW_GATEWAY_TAG=%r is not a valid format string (%s); using the DIN suffix",
            template,
            exc,
        )
        tag = ""
    return tag or sanitize_tag(suffix) or din


def _usable(template: Optional[str], setting: str) -> bool:
    if not template:
        return False
    if "{field}" not in template.replace("{FIELD}", "{field}"):
        _warn_once(setting + ":" + template, "%s=%r has no {field} placeholder; ignoring it", setting, template)
        return False
    try:
        render(template, {"tag": "t", "field": "f"})
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        _warn_once(setting + ":" + template, "%s=%r is not a valid format string (%s); ignoring it", setting, template, exc)
        return False
    return True


def category_format(settings, category: str) -> str:
    """Resolve the format for ``category``: its own setting, else the default setting, else built-in."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown naming category {category!r}")
    own = getattr(settings, f"gateway_{category}_format", None)
    if _usable(own, f"PW_GATEWAY_{category.upper()}_FORMAT"):
        return own
    default = getattr(settings, "gateway_field_format", None)
    if _usable(default, "PW_GATEWAY_FIELD_FORMAT"):
        return default
    return DEFAULT_FIELD_FORMAT


def field_name(field: str, tag: str, field_format: str = DEFAULT_FIELD_FORMAT) -> str:
    return render(field_format, {"tag": tag, "field": field})
