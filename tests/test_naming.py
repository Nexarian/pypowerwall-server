"""Configurable multi-gateway field naming (app.core.naming)."""
import logging

from app.core import naming
from app.models.gateway import Gateway


def _gw(gw_id="1538100-01-G--GF225311003KW7", name="KW7", tag=None):
    return Gateway(id=gw_id, name=name, host="10.0.0.1", tag=tag)


def test_render_leaves_unknown_placeholders():
    assert naming.render("{tag}_{field}_{nope}", {"tag": "A", "field": "B"}) == "A_B_{nope}"


def test_default_tag_is_gateway_name():
    assert naming.gateway_tag(_gw(), 2) == "KW7"


def test_tag_placeholders():
    gw = _gw()
    assert naming.gateway_tag(gw, 2, "{suffix}") == "KW7"
    assert naming.gateway_tag(gw, 2, "gw{index}") == "gw2"
    assert naming.gateway_tag(gw, 2, "{id}") == "1538100-01-G--GF225311003KW7"
    assert naming.gateway_tag(gw, 2, "{name}-{index}") == "KW7-2"


def test_tag_is_sanitized():
    assert naming.gateway_tag(_gw(name="Garage Roof (east)"), 1) == "Garage_Roof_east"


def test_explicit_gateway_tag_wins():
    assert naming.gateway_tag(_gw(tag="east"), 1, "{index}") == "east"


def test_empty_tag_falls_back_to_id_suffix():
    assert naming.gateway_tag(_gw(name="???"), 1) == "KW7"
    assert naming.gateway_tag(_gw(), 1, "") == "KW7"


def test_field_name_templates():
    assert naming.field_name("PVAC_Fout", "KW7") == "KW7_PVAC_Fout"
    assert naming.field_name("PVAC_Fout", "KW7", "{field}.{tag}") == "PVAC_Fout.KW7"
    assert naming.field_name("A", "KW7", "{tag}{field}") == "KW7A"


def test_field_format_without_field_placeholder_is_rejected(caplog):
    with caplog.at_level(logging.WARNING):
        assert naming.validate_field_format("{tag}", "{tag}_{field}", "X") == "{tag}_{field}"
        assert naming.validate_field_format("", "{tag}_{field}", "X") == "{tag}_{field}"
    assert "no {field} placeholder" in caplog.text
    assert naming.validate_field_format("{field}-{tag}", "{tag}_{field}", "X") == "{field}-{tag}"


def test_name_placeholder_uses_suffix_without_distinct_name():
    gw = _gw(name="1538100-01-G--GF225311003KW7")
    assert naming.gateway_tag(gw, 1) == "KW7"
    assert naming.gateway_tag(gw, 1, "{name}-{index}") == "KW7-1"
