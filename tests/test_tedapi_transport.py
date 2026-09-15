"""Tests for the TEDAPI transport settings (auth mode / API version).

Covers PW_TEDAPI_AUTH_MODE / PW_TEDAPI_API_VERSION and the per-gateway
``tedapi_auth_mode`` / ``tedapi_api_version`` overrides: config loading and
normalisation, lenient coercion at registration, the kwargs passed to
``pypowerwall.Powerwall()``, the registration / mismatch warnings, and the
``/stats`` + ``/health`` reporting.
"""
import importlib
import json
import logging
from unittest.mock import Mock

import pytest

from app.config import GatewayConfig, Settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway, GatewayStatus, PowerwallData


@pytest.fixture(autouse=True)
def _never_touch_the_network(monkeypatch, mock_pypowerwall):
    """gateway_manager.initialize() starts poll loops that would otherwise
    construct a real pypowerwall.Powerwall() against the test hosts."""
    import pypowerwall

    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pypowerwall)


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_settings_tedapi_transport_defaults(monkeypatch):
    monkeypatch.delenv("PW_TEDAPI_AUTH_MODE", raising=False)
    monkeypatch.delenv("PW_TEDAPI_API_VERSION", raising=False)
    settings = Settings()
    assert settings.tedapi_auth_mode == "basic"
    assert settings.tedapi_api_version == "V2024_06"


def test_settings_tedapi_transport_from_env(monkeypatch):
    monkeypatch.setenv("PW_TEDAPI_AUTH_MODE", "bearer")
    monkeypatch.setenv("PW_TEDAPI_API_VERSION", "V2026_06")
    settings = Settings()
    assert settings.tedapi_auth_mode == "bearer"
    assert settings.tedapi_api_version == "V2026_06"


def test_gateway_config_normalises_transport_values():
    config = GatewayConfig(
        id="gw",
        host="192.168.1.50",
        gw_pwd="pw",
        tedapi_auth_mode=" Bearer ",
        tedapi_api_version="v2026_06",
    )
    assert config.tedapi_auth_mode == "bearer"
    assert config.tedapi_api_version == "V2026_06"


def test_gateway_config_transport_defaults_to_none():
    config = GatewayConfig(id="gw", host="192.168.1.50", gw_pwd="pw")
    assert config.tedapi_auth_mode is None
    assert config.tedapi_api_version is None


def test_gateway_config_invalid_transport_value_does_not_raise():
    """A typo must not abort config loading; it is coerced at registration."""
    config = GatewayConfig(
        id="gw", host="192.168.1.50", gw_pwd="pw", tedapi_auth_mode="bearr"
    )
    assert config.tedapi_auth_mode == "bearr"


def test_legacy_single_gateway_inherits_global_transport(monkeypatch):
    monkeypatch.delenv("PW_CONFIG", raising=False)
    monkeypatch.delenv("PW_GATEWAYS", raising=False)
    monkeypatch.setenv("PW_HOST", "192.168.1.50")
    monkeypatch.setenv("PW_GW_PWD", "pw")
    monkeypatch.setenv("PW_TEDAPI_AUTH_MODE", "BEARER")
    monkeypatch.setenv("PW_TEDAPI_API_VERSION", "v2026_06")
    settings = Settings()
    assert len(settings.gateways) == 1
    assert settings.gateways[0].id == "default"
    # GatewayConfig normalises case/whitespace
    assert settings.gateways[0].tedapi_auth_mode == "bearer"
    assert settings.gateways[0].tedapi_api_version == "V2026_06"


def test_pw_gateways_per_gateway_override(monkeypatch):
    monkeypatch.delenv("PW_CONFIG", raising=False)
    monkeypatch.setenv(
        "PW_GATEWAYS",
        json.dumps(
            [
                {"id": "a", "host": "192.168.1.50", "gw_pwd": "pw"},
                {
                    "id": "b",
                    "host": "192.168.1.60",
                    "gw_pwd": "pw",
                    "tedapi_auth_mode": "bearer",
                    "tedapi_api_version": "V2026_06",
                },
            ]
        ),
    )
    settings = Settings()
    by_id = {gw.id: gw for gw in settings.gateways}
    assert by_id["a"].tedapi_auth_mode is None  # inherits global at registration
    assert by_id["b"].tedapi_auth_mode == "bearer"
    assert by_id["b"].tedapi_api_version == "V2026_06"


# ---------------------------------------------------------------------------
# Registration: resolution, coercion, warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_resolves_per_gateway_over_global(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tedapi_auth_mode", "basic")
    monkeypatch.setattr(settings, "tedapi_api_version", "V2024_06")
    configs = [
        GatewayConfig(id="inherit", host="192.168.1.50", gw_pwd="pw"),
        GatewayConfig(
            id="override",
            host="192.168.1.60",
            gw_pwd="pw",
            tedapi_auth_mode="bearer",
            tedapi_api_version="V2026_06",
        ),
    ]
    await gateway_manager.initialize(configs, poll_interval=5)
    try:
        assert gateway_manager.gateways["inherit"].tedapi_auth_mode == "basic"
        assert gateway_manager.gateways["inherit"].tedapi_api_version == "V2024_06"
        assert gateway_manager.gateways["override"].tedapi_auth_mode == "bearer"
        assert gateway_manager.gateways["override"].tedapi_api_version == "V2026_06"
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_registration_uses_global_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tedapi_auth_mode", "bearer")
    monkeypatch.setattr(settings, "tedapi_api_version", "v2026_06")  # un-normalised env value
    configs = [GatewayConfig(id="gw", host="192.168.1.50", gw_pwd="pw")]
    await gateway_manager.initialize(configs, poll_interval=5)
    try:
        assert gateway_manager.gateways["gw"].tedapi_auth_mode == "bearer"
        assert gateway_manager.gateways["gw"].tedapi_api_version == "V2026_06"
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_invalid_transport_values_fall_back_with_warning(monkeypatch, caplog):
    """Lenient coercion: pypowerwall.Powerwall() would raise ValueError on
    'bearr' and turn the gateway into a permanently failing poll."""
    configs = [
        GatewayConfig(
            id="typo",
            host="192.168.1.50",
            gw_pwd="pw",
            tedapi_auth_mode="bearr",
            tedapi_api_version="V2030_01",
        )
    ]
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        await gateway_manager.initialize(configs, poll_interval=5)
    try:
        gw = gateway_manager.gateways["typo"]
        assert gw.tedapi_auth_mode == "basic"
        assert gw.tedapi_api_version == "V2024_06"
        messages = [r.getMessage() for r in caplog.records]
        assert any("unknown tedapi_auth_mode 'bearr'" in m for m in messages)
        assert any("unknown tedapi_api_version 'V2030_01'" in m for m in messages)
    finally:
        await gateway_manager.shutdown()


@pytest.mark.parametrize(
    "config_kwargs, expected_fragment",
    [
        # Basic LAN: host + password only
        ({"password": "12345"}, "Basic LAN mode"),
        # Hybrid: gw_pwd + password
        ({"gw_pwd": "pw", "password": "12345"}, "hybrid mode"),
        # v1r: rsa_key_path is incompatible with bearer
        ({"gw_pwd": "pw", "rsa_key_path": "/keys/k.pem"}, "incompatible with bearer"),
    ],
)
@pytest.mark.asyncio
async def test_bearer_warns_when_pypowerwall_will_ignore_it(
    caplog, config_kwargs, expected_fragment
):
    configs = [
        GatewayConfig(
            id="gw", host="192.168.1.50", tedapi_auth_mode="bearer", **config_kwargs
        )
    ]
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        await gateway_manager.initialize(configs, poll_interval=5)
    try:
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "tedapi_auth_mode=bearer is ignored" in m and expected_fragment in m
            for m in messages
        ), messages
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_bearer_on_full_tedapi_gateway_does_not_warn(caplog):
    configs = [
        GatewayConfig(
            id="gw", host="192.168.1.50", gw_pwd="pw", tedapi_auth_mode="bearer"
        )
    ]
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        await gateway_manager.initialize(configs, poll_interval=5)
    try:
        assert not any("is ignored" in r.getMessage() for r in caplog.records)
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_v2026_warns_when_protobuf_too_old(monkeypatch, caplog):
    # app.core re-exports the singleton under the module's name, so resolve
    # the module object explicitly.
    gm = importlib.import_module("app.core.gateway_manager")

    monkeypatch.setattr(gm, "_protobuf_version", lambda: (4, 25, 1))
    configs = [
        GatewayConfig(
            id="gw", host="192.168.1.50", gw_pwd="pw", tedapi_api_version="V2026_06"
        )
    ]
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        await gateway_manager.initialize(configs, poll_interval=5)
    try:
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "needs protobuf >= 6.33.6" in m and "pip install 'protobuf>=6.33.6'" in m
            for m in messages
        ), messages
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_v2026_does_not_warn_when_protobuf_new_enough(monkeypatch, caplog):
    # app.core re-exports the singleton under the module's name, so resolve
    # the module object explicitly.
    gm = importlib.import_module("app.core.gateway_manager")

    monkeypatch.setattr(gm, "_protobuf_version", lambda: (6, 33, 6))
    configs = [
        GatewayConfig(
            id="gw", host="192.168.1.50", gw_pwd="pw", tedapi_api_version="V2026_06"
        )
    ]
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        await gateway_manager.initialize(configs, poll_interval=5)
    try:
        assert not any("needs protobuf" in r.getMessage() for r in caplog.records)
    finally:
        await gateway_manager.shutdown()


# ---------------------------------------------------------------------------
# Constructor wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_passed_to_powerwall_constructor(monkeypatch, mock_pypowerwall):
    """Mirror of test_rsa_key_path_passed_to_powerwall_constructor for the
    two TEDAPI transport kwargs."""
    import pypowerwall

    powerwall_spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)

    gw = Gateway(
        id="bearer-connect",
        name="Bearer gateway",
        host="192.168.1.50",
        gw_pwd="pw",
        tedapi_auth_mode="bearer",
        tedapi_api_version="V2026_06",
    )
    config = GatewayConfig(
        id="bearer-connect",
        host="192.168.1.50",
        gw_pwd="pw",
        tedapi_auth_mode="bearer",
        tedapi_api_version="V2026_06",
    )
    gateway_manager.gateways["bearer-connect"] = gw
    gateway_manager._pending_configs["bearer-connect"] = config
    gateway_manager.cache["bearer-connect"] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures["bearer-connect"] = 0
    gateway_manager._next_poll_time["bearer-connect"] = 0

    await gateway_manager._poll_gateway("bearer-connect")

    assert powerwall_spy.called, "pypowerwall.Powerwall() was never called"
    call_kwargs = powerwall_spy.call_args.kwargs
    assert call_kwargs.get("tedapi_auth_mode") == "bearer"
    assert call_kwargs.get("tedapi_api_version") == "V2026_06"
    assert call_kwargs.get("gw_pwd") == "pw"


@pytest.mark.asyncio
async def test_default_transport_passed_to_powerwall_constructor(
    monkeypatch, mock_pypowerwall
):
    import pypowerwall

    powerwall_spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)

    gw = Gateway(id="plain", name="Plain", host="192.168.91.1", gw_pwd="pw")
    config = GatewayConfig(id="plain", host="192.168.91.1", gw_pwd="pw")
    gateway_manager.gateways["plain"] = gw
    gateway_manager._pending_configs["plain"] = config
    gateway_manager.cache["plain"] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures["plain"] = 0
    gateway_manager._next_poll_time["plain"] = 0

    await gateway_manager._poll_gateway("plain")

    call_kwargs = powerwall_spy.call_args.kwargs
    assert call_kwargs.get("tedapi_auth_mode") == "basic"
    assert call_kwargs.get("tedapi_api_version") == "V2024_06"


# ---------------------------------------------------------------------------
# Active transport capture + mismatch warnings
# ---------------------------------------------------------------------------


def _registered_gateway(gateway_id="gw", **fields) -> Gateway:
    gw = Gateway(id=gateway_id, name=gateway_id, host="192.168.1.50", gw_pwd="pw", **fields)
    gateway_manager.gateways[gateway_id] = gw
    gateway_manager.cache[gateway_id] = GatewayStatus(gateway=gw, online=False)
    return gw


def test_active_transport_recorded_from_live_client():
    _registered_gateway(tedapi_auth_mode="bearer", tedapi_api_version="V2026_06")
    pw = Mock()
    pw.tedapi = Mock()
    pw.tedapi.auth_mode = "bearer"
    pw.tedapi_api_version = "V2026_06"
    data = PowerwallData()

    gateway_manager._record_active_transport("gw", pw, data)

    assert data.tedapi_auth_mode == "bearer"
    assert data.tedapi_api_version == "V2026_06"


def test_active_transport_ignores_non_string_attributes():
    """A Mock client (or one without the concept) must not leak repr() strings."""
    _registered_gateway()
    pw = Mock()  # pw.tedapi.auth_mode is an auto-created Mock, not a str
    data = PowerwallData()

    gateway_manager._record_active_transport("gw", pw, data)

    assert data.tedapi_auth_mode is None
    assert data.tedapi_api_version is None


def test_active_transport_mismatch_warned_once(caplog):
    _registered_gateway(tedapi_auth_mode="bearer")
    pw = Mock()
    pw.tedapi = Mock()
    pw.tedapi.auth_mode = "basic"  # e.g. hybrid mode speaks basic
    data = PowerwallData()

    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        gateway_manager._record_active_transport("gw", pw, data)
        gateway_manager._record_active_transport("gw", pw, data)

    mismatch = [
        r for r in caplog.records
        if "requested tedapi_auth_mode=bearer but the active" in r.getMessage()
    ]
    assert len(mismatch) == 1
    assert data.tedapi_auth_mode == "basic"


def test_bearer_on_pw3_warns_once(caplog):
    _registered_gateway(tedapi_auth_mode="bearer")
    pw = Mock()
    pw.tedapi = Mock()
    pw.tedapi.auth_mode = "bearer"
    data = PowerwallData(pw3=True)

    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        gateway_manager._record_active_transport("gw", pw, data)
        gateway_manager._record_active_transport("gw", pw, data)

    pw3_warnings = [
        r for r in caplog.records
        if "not supported on Powerwall 3" in r.getMessage()
    ]
    assert len(pw3_warnings) == 1


def test_tedapi_transport_helper_for_tedapi_gateway():
    gw = _registered_gateway(tedapi_auth_mode="bearer", tedapi_api_version="V2026_06")
    # Before the first poll: active unknown, effective = requested
    info = gateway_manager.tedapi_transport("gw")
    assert info["requested_auth_mode"] == "bearer"
    assert info["active_auth_mode"] is None
    assert info["auth_mode"] == "bearer"
    assert info["api_version"] == "V2026_06"

    # After a poll reported the active values
    gateway_manager.cache["gw"] = GatewayStatus(
        gateway=gw,
        online=True,
        data=PowerwallData(tedapi_auth_mode="basic", tedapi_api_version="V2024_06"),
    )
    info = gateway_manager.tedapi_transport("gw")
    assert info["active_auth_mode"] == "basic"
    assert info["auth_mode"] == "basic"
    assert info["api_version"] == "V2024_06"


@pytest.mark.parametrize(
    "fields",
    [
        {"host": None, "gw_pwd": None, "email": "user@example.com", "cloud_mode": True},
        {"host": None, "gw_pwd": None, "email": "user@example.com", "fleetapi": True},
        {"gw_pwd": None, "basic_lan": True},
    ],
)
def test_tedapi_transport_helper_none_for_non_tedapi_gateways(fields):
    base = {"id": "other", "name": "other", "host": "10.0.0.5", "gw_pwd": "pw"}
    base.update(fields)
    gw = Gateway(**base)
    gateway_manager.gateways["other"] = gw
    gateway_manager.cache["other"] = GatewayStatus(gateway=gw, online=False)

    info = gateway_manager.tedapi_transport("other")
    assert all(value is None for value in info.values())


def test_tedapi_transport_helper_unknown_gateway():
    info = gateway_manager.tedapi_transport("nope")
    assert all(value is None for value in info.values())


# ---------------------------------------------------------------------------
# /stats, /health, /api/gateways
# ---------------------------------------------------------------------------


def test_stats_reports_tedapi_transport(client, connected_gateway, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tedapi_auth_mode", "basic")
    monkeypatch.setattr(settings, "tedapi_api_version", "V2024_06")

    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["config"]["PW_TEDAPI_AUTH_MODE"] == "basic"
    assert data["config"]["PW_TEDAPI_API_VERSION"] == "V2024_06"
    assert data["config"]["PW_NEG_SOLAR"] is False
    assert data["tedapi_auth_mode"] == "basic"
    assert data["tedapi_api_version"] == "V2024_06"
    gw_status = data["gateway_statuses"][0]
    assert gw_status["tedapi_auth_mode"] == "basic"
    assert gw_status["tedapi_auth_mode_active"] is None
    assert gw_status["tedapi_api_version"] == "V2024_06"


def test_stats_reports_active_bearer_transport(client, connected_gateway):
    gw = gateway_manager.gateways["test-gateway"]
    gw.tedapi_auth_mode = "bearer"
    gw.tedapi_api_version = "V2026_06"
    status = gateway_manager.cache["test-gateway"]
    status.data.tedapi_auth_mode = "bearer"
    status.data.tedapi_api_version = "V2026_06"

    data = client.get("/stats").json()
    assert data["tedapi_auth_mode"] == "bearer"
    assert data["tedapi_api_version"] == "V2026_06"
    gw_status = data["gateway_statuses"][0]
    assert gw_status["tedapi_auth_mode_active"] == "bearer"
    assert gw_status["tedapi_api_version_active"] == "V2026_06"


def test_health_reports_tedapi_transport(client, connected_gateway):
    gw = gateway_manager.gateways["test-gateway"]
    gw.tedapi_auth_mode = "bearer"

    data = client.get("/health").json()
    detail = data["gateway_details"][0]
    assert detail["id"] == "test-gateway"
    assert detail["auth_mode"] == "bearer"
    assert detail["tedapi_api_version"] == "V2024_06"


def test_api_gateways_includes_transport(client, connected_gateway):
    gw = gateway_manager.gateways["test-gateway"]
    gw.tedapi_auth_mode = "bearer"
    gw.tedapi_api_version = "V2026_06"

    data = client.get("/api/gateways/test-gateway").json()
    gateway = data.get("gateway", data)
    assert gateway["tedapi_auth_mode"] == "bearer"
    assert gateway["tedapi_api_version"] == "V2026_06"
