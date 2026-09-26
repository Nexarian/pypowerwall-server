"""Configurable multi-gateway field naming (app.core.naming)."""
import logging
from types import SimpleNamespace

import pytest

from app.core import naming
from app.models.gateway import Gateway

DIN = "1538100-01-G--GF225311003KW7"


def _gw(gw_id=DIN, name="KW7", tag=None, host="10.0.0.1"):
    return Gateway(id=gw_id, name=name, host=host, tag=tag)


def _settings(**kw):
    base = dict(gateway_default_format="{tag}_{key}", gateway_solar_string_format=None,
                gateway_alert_format=None, gateway_freq_format=None)
    base.update(kw)
    return SimpleNamespace(**base)


# --- render / str.format semantics -------------------------------------------

def test_render_is_python_format():
    assert naming.render("{tag}_{key}", {"tag": "A", "key": "B"}) == "A_B"
    assert naming.render("{index:02d}", {"index": 3}) == "03"
    assert naming.render("{name:>5}", {"name": "ab"}) == "   ab"
    assert naming.render("{{literal}}{tag}", {"tag": "x"}) == "{literal}x"


def test_render_slices_strings_with_integer_spec():
    assert naming.render("{din:-3}", {"din": DIN}) == "KW7"
    assert naming.render("{din:7}", {"din": DIN}) == "1538100"


def test_render_placeholders_are_case_insensitive():
    assert naming.render("{DIN:-3}_{Key}", {"din": DIN, "key": "A"}) == "KW7_A"


def test_render_rejects_unknown_and_attribute_access():
    with pytest.raises(KeyError):
        naming.render("{nope}", {"tag": "x"})
    with pytest.raises(KeyError):
        naming.render("{tag.__class__}", {"tag": "x"})


# --- gateway_tag ---------------------------------------------------------------

def test_default_tag_is_gateway_name():
    assert naming.gateway_tag(_gw(), 2) == "KW7"


def test_tag_placeholders():
    gw = _gw()
    assert naming.gateway_tag(gw, 2, "{suffix}") == "KW7"
    assert naming.gateway_tag(gw, 2, "{din:-3}") == "KW7"
    assert naming.gateway_tag(gw, 2, "{DIN:-3}") == "KW7"
    assert naming.gateway_tag(gw, 2, "gw{index}") == "gw2"
    assert naming.gateway_tag(gw, 2, "gw{index:02d}") == "gw02"
    assert naming.gateway_tag(gw, 2, "{id}") == DIN
    assert naming.gateway_tag(gw, 2, "{din}") == DIN
    assert naming.gateway_tag(gw, 2, "{name}-{index}") == "KW7-2"
    assert naming.gateway_tag(gw, 2, "{host}") == "10_0_0_1"


def test_name_placeholder_uses_suffix_without_distinct_name():
    gw = _gw(name=DIN)
    assert naming.gateway_tag(gw, 1) == "KW7"
    assert naming.gateway_tag(gw, 1, "{name}-{index}") == "KW7-1"


def test_tag_is_sanitized():
    assert naming.gateway_tag(_gw(name="Garage Roof (east)"), 1) == "Garage_Roof_east"


def test_explicit_gateway_tag_wins():
    assert naming.gateway_tag(_gw(tag="east"), 1, "{index}") == "east"


def test_empty_or_invalid_tag_falls_back_to_din_suffix(caplog):
    assert naming.gateway_tag(_gw(name="???"), 1) == "KW7"
    assert naming.gateway_tag(_gw(), 1, "") == "KW7"
    with caplog.at_level(logging.WARNING):
        assert naming.gateway_tag(_gw(), 1, "{nope}") == "KW7"
        assert naming.gateway_tag(_gw(), 1, "{din") == "KW7"
    assert "not a valid format string" in caplog.text


# --- category formats ----------------------------------------------------------

def test_key_name_templates():
    assert naming.key_name("PVAC_Fout", "KW7") == "KW7_PVAC_Fout"
    assert naming.key_name("PVAC_Fout", "KW7", "{key}.{tag}") == "PVAC_Fout.KW7"
    assert naming.key_name("A", "KW7", "{tag}{key}") == "KW7A"
    assert naming.key_name("A", "KW7", "{KEY}_{TAG}") == "A_KW7"


def test_category_falls_back_to_default_then_builtin():
    s = _settings()
    for category in naming.CATEGORIES:
        assert naming.category_format(s, category) == "{tag}_{key}"
    s = _settings(gateway_default_format="{key}.{tag}")
    assert naming.category_format(s, "alert") == "{key}.{tag}"
    s = _settings(gateway_default_format="")
    assert naming.category_format(s, "alert") == naming.DEFAULT_FORMAT


def test_category_override_beats_default():
    s = _settings(gateway_solar_string_format="{key}_{tag}", gateway_default_format="{tag}_{key}")
    assert naming.category_format(s, "solar_string") == "{key}_{tag}"
    assert naming.category_format(s, "alert") == "{tag}_{key}"
    assert naming.category_format(s, "freq") == "{tag}_{key}"


def test_unusable_formats_are_skipped(caplog):
    with caplog.at_level(logging.WARNING):
        s = _settings(gateway_alert_format="{tag}", gateway_default_format="{key}-{tag}")
        assert naming.category_format(s, "alert") == "{key}-{tag}"
        s = _settings(gateway_freq_format="{key}{", gateway_default_format="{tag}")
        assert naming.category_format(s, "freq") == naming.DEFAULT_FORMAT
    assert "no {key} placeholder" in caplog.text
    assert "not a valid format string" in caplog.text


def test_unknown_category():
    with pytest.raises(ValueError):
        naming.category_format(_settings(), "pod")
