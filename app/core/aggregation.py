"""
Meter Aggregate Combination

Merges the ``/api/meters/aggregates`` documents of several gateways into one
document in the same Tesla format, so the legacy endpoints (``/aggregates``,
``/api/meters/aggregates``) and the ``/api/aggregate`` totals describe the whole
system when more than one gateway is configured.

Rules (the same ones the multi-instance pypowerwall proxy used):
    - Numeric readings of the same category (site, solar, load, battery) are
      summed; ``None`` counts as 0.
    - ``instant_average_voltage`` keeps the first non-zero reading, i.e. the
      gateway that owns the site meter.
    - Strings/timestamps keep the first value seen.
    - Home load is derived from the identity ``load = site + solar + battery``.
      A gateway without its own site CT (a second solar inverter feeding the same
      house) reports ``load = 0`` even while producing, so summing per-gateway
      load readings under-counts consumption. The identity holds per gateway and
      therefore for the sum.
    - Currents are derived from apparent power and the site voltage so that
      ``site + solar = load`` stays additive and per-phase site currents follow
      the site power sign (negative = exporting).

With a single document nothing is derived — the gateway's own readings are
returned untouched.
"""
import copy
import math
from typing import Any, Dict, Iterable, List, Optional

SKIP_KEYS = ("timeout", "disclaimer")
PREFER_NONZERO_KEYS = ("instant_average_voltage",)
PHASE_CURRENT_KEYS = ("i_a_current", "i_b_current", "i_c_current")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _merge_category(dest: Dict[str, Any], src: Dict[str, Any]) -> None:
    for key, value in src.items():
        if key in SKIP_KEYS:
            dest.setdefault(key, value)
        elif key in PREFER_NONZERO_KEYS:
            if not dest.get(key) and value:
                dest[key] = value
            elif key not in dest:
                dest[key] = value
        elif _is_number(value):
            current = dest.get(key)
            dest[key] = (current if _is_number(current) else 0) + value
        else:
            # None / strings / bools: keep the first value, but never shadow a number
            if key not in dest or dest[key] is None:
                dest[key] = value


def _num(readings: Dict[str, Any], key: str) -> float:
    value = readings.get(key)
    return value if _is_number(value) else 0


def _derive_power_metrics(combined: Dict[str, Any]) -> None:
    site = combined.get("site")
    solar = combined.get("solar")
    load = combined.get("load")
    if not (isinstance(site, dict) and isinstance(solar, dict) and isinstance(load, dict)):
        return
    battery = combined.get("battery") if isinstance(combined.get("battery"), dict) else {}

    site_power = _num(site, "instant_power")
    site_current = _num(site, "instant_average_current")
    site_reactive = _num(site, "instant_reactive_power")
    site_apparent = _num(site, "instant_apparent_power")
    solar_power = _num(solar, "instant_power")
    solar_reactive = _num(solar, "instant_reactive_power")
    solar_apparent = _num(solar, "instant_apparent_power")
    battery_power = _num(battery, "instant_power")
    battery_reactive = _num(battery, "instant_reactive_power")

    # Home load from the power-flow identity (see module docstring)
    load_power = site_power + solar_power + battery_power
    load_reactive = site_reactive + solar_reactive + battery_reactive
    load_apparent = (
        math.sqrt(load_power**2 + load_reactive**2) if load_reactive else abs(load_power)
    )
    load["instant_power"] = load_power
    load["instant_reactive_power"] = load_reactive
    load["instant_apparent_power"] = load_apparent

    # Per-phase site currents follow the site power sign
    if _num(site, "instant_total_current") and site_power:
        for phase_key in PHASE_CURRENT_KEYS:
            phase_val = _num(site, phase_key)
            if phase_val:
                site[phase_key] = math.copysign(abs(phase_val), site_power)

    site_voltage = _num(site, "instant_average_voltage")
    if not site_voltage:
        site_voltage = abs(site_power) / abs(site_current) if site_current else 0
        site["instant_average_voltage"] = site_voltage

    def _current(apparent: float, power: float) -> float:
        if not site_voltage:
            return 0
        return (apparent if apparent else abs(power)) / site_voltage

    site_current_mag = _current(site_apparent, site_power)
    site["instant_total_current"] = (
        math.copysign(site_current_mag, site_power) if site_power else 0
    )
    solar["instant_total_current"] = _current(solar_apparent, solar_power)
    load["instant_average_voltage"] = site_voltage
    load["instant_total_current"] = load_apparent / site_voltage if site_voltage else 0


def combine_meter_aggregates(
    results: Iterable[Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Combine per-gateway ``/api/meters/aggregates`` documents into one.

    Returns ``{}`` when no gateway has data, the single document unchanged when
    only one gateway reports, and the merged/derived document otherwise.
    """
    documents: List[Dict[str, Any]] = [r for r in results if isinstance(r, dict) and r]
    if not documents:
        return {}
    if len(documents) == 1:
        return copy.deepcopy(documents[0])

    combined: Dict[str, Any] = {}
    for document in documents:
        for category, readings in document.items():
            if isinstance(readings, dict):
                _merge_category(combined.setdefault(category, {}), readings)
            else:
                combined.setdefault(category, readings)
    _derive_power_metrics(combined)
    return combined
