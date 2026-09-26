"""Legacy endpoints in multi-gateway mode.

Two gateways feed one house: "1JG" owns the site meter (Neurio CTs on the grid
connection), "KW7" is a second solar-only inverter with only a solar CT, so it
reports site=0 and load=0 while producing. The legacy endpoints must describe
the whole system in one document with per-gateway field names that cannot
collide (the layout the Powerwall-Dashboard continuous queries were built on).
"""
import pytest

from app.core.aggregation import combine_meter_aggregates
from app.models.gateway import Gateway, GatewayStatus, PowerwallData


def _meters(site, solar, load, site_voltage=None, site_phase=None, solar_voltage=None):
    doc = {
        "site": {
            "instant_power": site,
            "instant_reactive_power": 0,
            "instant_apparent_power": 0,
            "instant_average_voltage": site_voltage,
            "instant_average_current": (site / site_voltage) if site_voltage else None,
            "i_a_current": site_phase,
            "i_b_current": site_phase,
            "i_c_current": 0,
            "instant_total_current": (site / site_voltage) if site_voltage else None,
            "timeout": 1500000000,
            "last_communication_time": "2026-09-26T14:01:48-04:00",
        },
        "battery": {
            "instant_power": 0,
            "instant_reactive_power": 0,
            "instant_apparent_power": 0,
            "instant_average_voltage": None,
            "instant_total_current": None,
        },
        "load": {
            "instant_power": load,
            "instant_reactive_power": 0,
            "instant_apparent_power": 0,
            "instant_average_voltage": None,
            "instant_total_current": None,
        },
        "solar": {
            "instant_power": solar,
            "instant_reactive_power": 0,
            "instant_apparent_power": 0,
            "instant_average_voltage": solar_voltage,
            "instant_total_current": (solar / solar_voltage) if solar_voltage else None,
        },
    }
    return doc


def _vitals(pvac_serial, fout):
    return {
        f"PVAC--1538100-01-F--{pvac_serial}": {
            "PVAC_Fout": fout,
            "PVAC_VL1Ground": 121.0,
            "PVAC_VL2Ground": 122.0,
            "PVAC_Fan_Speed_Actual_RPM": 1500,
            "PVAC_Pout": 4000,  # not a /freq field
        },
        "TESYNC--None--None": {
            "ISLAND_FreqL1_Main": 60.0,
            "METER_X_CTA_I": None,
            "ISLAND_GridConnected": "ISLAND_GridConnected_Island",
        },
    }


@pytest.fixture
def two_inverters(mock_gateway_manager):
    """1JG (site meter, 4 strings) + KW7 (solar only, no site CT)."""

    def _add(gw_id, name, aggregates, strings, vitals, alerts, grid="UP", temps=None, soe=None):
        gw = Gateway(id=gw_id, name=name, host="10.0.0.1", gw_pwd="x", online=True, type="inverter")
        data = PowerwallData(
            aggregates=aggregates,
            strings=strings,
            vitals=vitals,
            alerts=alerts,
            grid_status=grid,
            temps=temps or {},
            soe=soe,
            soe_raw=soe,
            timestamp=1234567890.0,
        )
        status = GatewayStatus(gateway=gw, data=data, online=True, last_updated=1234567890.0)
        mock_gateway_manager.gateways[gw_id] = gw
        mock_gateway_manager.cache[gw_id] = status
        return status

    a = _add(
        "1538000-45-C--GF2240650001JG",
        "1JG",
        _meters(site=-2000.0, solar=4000.0, load=2000.0, site_voltage=240.0, site_phase=5.0, solar_voltage=240.0),
        {"A": {"Current": 3.0, "Power": 600.0, "Voltage": 200.0, "State": "PV_Active", "Connected": True}},
        _vitals("AAA", 60.01),
        ["IslandChecksFailed", "SystemConnectedToGrid"],
    )
    b = _add(
        "1538100-01-G--GF225311003KW7",
        "KW7",
        _meters(site=0, solar=3000.0, load=0, solar_voltage=241.0),
        {"A": {"Current": 4.0, "Power": 900.0, "Voltage": 225.0, "State": "PV_Active", "Connected": True}},
        _vitals("BBB", 59.99),
        ["IslandChecksFailed", "PVS_a060_MciClose"],
    )
    return a, b


# --------------------------------------------------------------------------- #
# app.core.aggregation
# --------------------------------------------------------------------------- #

def test_combine_single_document_is_unchanged():
    doc = _meters(site=-2000.0, solar=4000.0, load=1500.0, site_voltage=240.0)
    combined = combine_meter_aggregates([doc])
    assert combined == doc
    assert combined is not doc  # copy, caller cannot mutate the cache


def test_combine_empty():
    assert combine_meter_aggregates([]) == {}
    assert combine_meter_aggregates([None, {}]) == {}


def test_combine_derives_home_load_from_site_solar_battery():
    a = _meters(site=-2000.0, solar=4000.0, load=2000.0, site_voltage=240.0, site_phase=5.0, solar_voltage=240.0)
    b = _meters(site=0, solar=3000.0, load=0, solar_voltage=241.0)
    combined = combine_meter_aggregates([a, b])

    assert combined["solar"]["instant_power"] == 7000.0
    assert combined["site"]["instant_power"] == -2000.0
    # Summing per-gateway load (2000 + 0) would hide KW7's production;
    # the house actually consumes site + solar + battery.
    assert combined["load"]["instant_power"] == 5000.0
    assert combined["load"]["instant_apparent_power"] == 5000.0

    # Voltage comes from the gateway that owns the site meter
    assert combined["site"]["instant_average_voltage"] == 240.0
    assert combined["load"]["instant_average_voltage"] == 240.0
    # Currents derived against the site voltage so site + solar = load
    assert combined["site"]["instant_total_current"] == pytest.approx(-2000.0 / 240.0)
    assert combined["solar"]["instant_total_current"] == pytest.approx(7000.0 / 240.0)
    assert combined["load"]["instant_total_current"] == pytest.approx(5000.0 / 240.0)
    # Per-phase site currents follow the export sign
    assert combined["site"]["i_a_current"] == -5.0
    # Non-numeric readings keep the first value; timeout is not summed
    assert combined["site"]["timeout"] == 1500000000
    assert combined["site"]["last_communication_time"] == "2026-09-26T14:01:48-04:00"


def test_combine_none_does_not_shadow_numbers():
    a = {"site": {"instant_power": None, "instant_average_voltage": None}}
    b = {"site": {"instant_power": 10.0, "instant_average_voltage": 240.0}}
    combined = combine_meter_aggregates([a, b])
    assert combined["site"]["instant_power"] == 10.0
    assert combined["site"]["instant_average_voltage"] == 240.0


# --------------------------------------------------------------------------- #
# Legacy endpoints
# --------------------------------------------------------------------------- #

def test_aggregates_are_merged(client, two_inverters):
    for path in ("/aggregates", "/api/meters/aggregates"):
        data = client.get(path).json()
        assert data["solar"]["instant_power"] == 7000.0
        assert data["load"]["instant_power"] == 5000.0
        assert data["site"]["instant_power"] == -2000.0


def test_api_aggregate_totals_use_merged_load(client, two_inverters):
    data = client.get("/api/aggregate/").json()
    assert data["total_solar_power"] == 7000.0
    assert data["total_site_power"] == -2000.0
    assert data["total_load_power"] == 5000.0
    power = client.get("/api/aggregate/power").json()
    assert power["load"] == 5000.0


def test_solar_strings_are_tagged_per_gateway(client, two_inverters):
    data = client.get("/strings").json()
    assert set(data) == {"1JG_A", "KW7_A"}
    assert data["1JG_A"]["Power"] == 600.0
    assert data["KW7_A"]["Power"] == 900.0


def test_freq_is_prefixed_per_gateway(client, two_inverters):
    data = client.get("/freq").json()
    assert data["1JG_PVAC_Fout"] == 60.01
    assert data["KW7_PVAC_Fout"] == 59.99
    assert data["1JG_PVAC_VL1Ground"] == 121.0
    assert data["KW7_PVAC_Fan_Speed_Actual_RPM"] == 1500
    assert data["1JG_ISLAND_FreqL1_Main"] == 60.0
    assert "1JG_PVAC_Pout" not in data
    assert "PVAC_Fout" not in data
    assert data["grid_status"] == 1


def test_freq_grid_status_is_worst_gateway(client, two_inverters):
    _, kw7 = two_inverters
    kw7.data.grid_status = "DOWN"
    assert client.get("/freq").json()["grid_status"] == 0


def test_freq_single_gateway_includes_pvac_unprefixed(client, connected_gateway):
    connected_gateway.data.vitals["PVAC--1538100-01-F--AAA"] = {"PVAC_Fout": 60.02, "PVAC_Pout": 1}
    data = client.get("/freq").json()
    assert data["PVAC_Fout"] == 60.02
    assert "PVAC_Pout" not in data
    assert data["PW1_f_out"] == 60.0  # existing fields untouched


def test_alerts_are_prefixed_per_gateway(client, two_inverters):
    assert client.get("/alerts").json() == [
        "1JG_IslandChecksFailed",
        "1JG_SystemConnectedToGrid",
        "KW7_IslandChecksFailed",
        "KW7_PVS_a060_MciClose",
    ]
    assert client.get("/alerts/pw").json() == {
        "1JG_IslandChecksFailed": 1,
        "1JG_SystemConnectedToGrid": 1,
        "KW7_IslandChecksFailed": 1,
        "KW7_PVS_a060_MciClose": 1,
    }


def test_soe_is_null_without_batteries(client, two_inverters):
    assert client.get("/soe").json() == {"percentage": None, "raw_percentage": None}


def test_soe_averages_reporting_gateways(client, two_inverters):
    a, _ = two_inverters
    a.data.soe = 40.0
    a.data.soe_raw = 42.0
    data = client.get("/soe").json()
    assert data == {"percentage": 40.0, "raw_percentage": 42.0}


def test_temps_pw_numbering_continues(client, two_inverters):
    a, b = two_inverters
    a.data.temps = {"TEPOD--1": 20.0}
    b.data.temps = {"TEPOD--2": 21.0, "TEPOD--3": 22.0}
    assert client.get("/temps/pw").json() == {
        "PW1_temp": 20.0,
        "PW2_temp": 21.0,
        "PW3_temp": 22.0,
    }


def test_pod_numbering_continues(client, two_inverters, mock_pypowerwall):
    a, b = two_inverters
    a.data.system_status = mock_pypowerwall.system_status.return_value
    b.data.system_status = mock_pypowerwall.system_status.return_value
    a.data.time_remaining = 8.0
    b.data.time_remaining = 3.0
    b.data.reserve = 25
    data = client.get("/pod").json()
    assert data["PW1_PackageSerialNumber"] == "TG1234567890AB"
    assert data["PW2_PackageSerialNumber"] == "TG1234567890AB"
    assert data["nominal_full_pack_energy"] == 27000
    assert data["nominal_energy_remaining"] == 2 * 11547
    assert data["time_remaining_hours"] == 3.0
    assert data["backup_reserve_percent"] == 25


def test_gateway_tag_falls_back_to_id_suffix(client, two_inverters):
    a, _ = two_inverters
    a.gateway.name = a.gateway.id  # no distinct name configured
    data = client.get("/strings").json()
    assert "1JG_A" in data  # last three characters of the id


def test_gateway_tag_is_sanitized(client, two_inverters):
    a, _ = two_inverters
    a.gateway.name = "Garage Roof (east)"
    assert "Garage_Roof_east_A" in client.get("/strings").json()


# --------------------------------------------------------------------------- #
# Configurable naming (PW_GATEWAY_TAG, PW_GATEWAY_FIELD_FORMAT, per-category overrides)
# --------------------------------------------------------------------------- #

def test_solar_string_format_override(client, two_inverters, monkeypatch):
    """Powerwall-Dashboard's layout: strings suffixed, everything else prefixed."""
    from app.config import settings
    monkeypatch.setattr(settings, "gateway_solar_string_format", "{field}_{tag}")
    assert set(client.get("/strings").json()) == {"A_1JG", "A_KW7"}
    assert "1JG_PVAC_Fout" in client.get("/freq").json()
    assert "1JG_IslandChecksFailed" in client.get("/alerts/pw").json()


def test_default_field_format_applies_to_every_category(client, two_inverters, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gateway_field_format", "{field}.{tag}")
    assert "A.1JG" in client.get("/strings").json()
    freq = client.get("/freq").json()
    assert freq["PVAC_Fout.1JG"] == 60.01
    assert freq["ISLAND_FreqL1_Main.KW7"] == 60.0
    assert freq["grid_status"] == 1
    assert client.get("/alerts").json()[0] == "IslandChecksFailed.1JG"
    assert "PVS_a060_MciClose.KW7" in client.get("/alerts/pw").json()


def test_alert_and_freq_overrides(client, two_inverters, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gateway_alert_format", "{tag}:{field}")
    monkeypatch.setattr(settings, "gateway_freq_format", "{field}@{TAG}")
    assert "KW7:IslandChecksFailed" in client.get("/alerts/pw").json()
    assert "PVAC_Fout@KW7" in client.get("/freq").json()
    assert "KW7_A" in client.get("/strings").json()  # untouched category keeps the default


def test_tag_format_setting(client, two_inverters, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gateway_tag_format", "inv{index:02d}")
    assert set(client.get("/strings").json()) == {"inv01_A", "inv02_A"}
    assert "inv02_PVAC_Fout" in client.get("/freq").json()
    monkeypatch.setattr(settings, "gateway_tag_format", "{DIN:-3}")
    assert set(client.get("/strings").json()) == {"1JG_A", "KW7_A"}


def test_per_gateway_tag_overrides_template(client, two_inverters, monkeypatch):
    from app.config import settings
    a, _ = two_inverters
    monkeypatch.setattr(settings, "gateway_tag_format", "{suffix}")
    a.gateway.tag = "east"
    assert set(client.get("/strings").json()) == {"east_A", "KW7_A"}


def test_bad_field_format_falls_back_to_default(client, two_inverters, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gateway_freq_format", "{tag}")
    monkeypatch.setattr(settings, "gateway_field_format", "{field}{")
    assert "1JG_PVAC_Fout" in client.get("/freq").json()


def test_stats_reports_naming_settings(client, two_inverters):
    config = client.get("/stats").json()["config"]
    assert config["PW_GATEWAY_TAG"] == "{name}"
    assert config["PW_GATEWAY_FIELD_FORMAT"] == "{tag}_{field}"
    for key in ("PW_GATEWAY_SOLAR_STRING_FORMAT", "PW_GATEWAY_ALERT_FORMAT", "PW_GATEWAY_FREQ_FORMAT"):
        assert key in config


def test_settings_read_naming_env(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("PW_GATEWAY_TAG", "{din:-3}")
    monkeypatch.setenv("PW_GATEWAY_FIELD_FORMAT", "{field}__{tag}")
    monkeypatch.setenv("PW_GATEWAY_SOLAR_STRING_FORMAT", "{tag}{field}")
    monkeypatch.setenv("PW_GATEWAY_ALERT_FORMAT", "{tag}:{field}")
    monkeypatch.setenv("PW_GATEWAY_FREQ_FORMAT", "{field}@{tag}")
    s = Settings()
    assert s.gateway_tag_format == "{din:-3}"
    assert s.gateway_field_format == "{field}__{tag}"
    assert s.gateway_solar_string_format == "{tag}{field}"
    assert s.gateway_alert_format == "{tag}:{field}"
    assert s.gateway_freq_format == "{field}@{tag}"


def test_settings_naming_defaults():
    from app.config import Settings
    s = Settings()
    assert s.gateway_tag_format == "{name}"
    assert s.gateway_field_format == "{tag}_{field}"
    assert s.gateway_solar_string_format is None
    assert s.gateway_alert_format is None
    assert s.gateway_freq_format is None


def test_gateway_config_tag_reaches_gateway_model():
    from app.config import GatewayConfig
    cfg = GatewayConfig(id="x", host="10.0.0.1", tag="east")
    assert cfg.tag == "east"
