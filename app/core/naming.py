"""
Multi-Gateway Field Naming

When more than one gateway is configured, the legacy endpoints (/strings,
/freq, /alerts, /alerts/pw) return every gateway's data in one document and
need a per-gateway label in each key so they cannot collide. There is no
agreed layout for those keys, so it is configurable:

    PW_GATEWAY_TAG            template for the per-gateway label
                              placeholders: {name} {id} {suffix} {index}
                              default "{name}"
    PW_GATEWAY_FIELD_FORMAT   template for /freq and alert fields
                              placeholders: {tag} {field}
                              default "{tag}_{field}"      -> "1JG_PVAC_Fout"
    PW_GATEWAY_STRING_FORMAT  template for /strings keys
                              placeholders: {tag} {field}
                              default "{field}_{tag}"      -> "A_1JG"

    {suffix} is the last three characters of the gateway id (the DIN suffix
    the old proxy used); {name} falls back to {suffix} when the gateway has no
    distinct name; {index} is the gateway's 1-based position in PW_GATEWAYS. A gateway can also set an explicit "tag" in PW_GATEWAYS,
    which bypasses PW_GATEWAY_TAG.

Rendered tags are sanitized to [A-Za-z0-9_-] (anything else becomes "_") so
they are safe as InfluxDB field keys and in Grafana queries. A field template
without {field} would produce colliding keys, so it is rejected at load time
and the default is used instead (with a logged warning).
"""
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

DEFAULT_TAG_FORMAT = "{name}"
DEFAULT_FIELD_FORMAT = "{tag}_{field}"
DEFAULT_STRING_FORMAT = "{field}_{tag}"

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def render(template: str, values: Dict[str, Any]) -> str:
    """Substitute ``{name}`` placeholders; unknown placeholders are left as-is."""
    return _PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template,
    )


def sanitize_tag(tag: str) -> str:
    return _UNSAFE.sub("_", tag).strip("_")


def validate_field_format(template: str, default: str, setting: str) -> str:
    """Return ``template`` if it contains ``{field}``, otherwise ``default``."""
    if template and "{field}" in template:
        return template
    logger.warning(
        "%s=%r has no {field} placeholder; using %r", setting, template, default
    )
    return default


def gateway_tag(gateway, index: int, tag_format: str = DEFAULT_TAG_FORMAT) -> str:
    """Per-gateway label for multi-gateway field names.

    An explicit ``gateway.tag`` wins; otherwise ``tag_format`` is rendered with
    the gateway's name, id, id suffix and 1-based index. An empty result falls
    back to the id suffix so keys never lose their gateway label.
    """
    explicit = getattr(gateway, "tag", None)
    if explicit:
        tag = sanitize_tag(str(explicit))
        if tag:
            return tag
    suffix = gateway.id[-3:]
    # A gateway without a distinct name reports name == id; use the id suffix
    # for {name} then, so the default template never yields a full DIN.
    name = gateway.name if gateway.name and gateway.name != gateway.id else suffix
    tag = sanitize_tag(
        render(
            tag_format or DEFAULT_TAG_FORMAT,
            {"name": name, "id": gateway.id, "suffix": suffix, "index": index},
        )
    )
    return tag or sanitize_tag(suffix) or gateway.id


def field_name(field: str, tag: str, field_format: str = DEFAULT_FIELD_FORMAT) -> str:
    return render(field_format, {"tag": tag, "field": field})
