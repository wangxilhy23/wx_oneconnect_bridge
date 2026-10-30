"""Huawei ``fan`` service: fan gear and swing direction.

Default contract source
    prodId    : ``A0NQ`` -- wall-mounted air conditioner
    Profile   : https://smarthome-drcn.dbankcdn.com/device/guide/A0NQ/A0NQ.json
    common    : yes -- air conditioners expose ``gear`` / ``direction`` enums
    verified  : candidate, 2026-10-22 -- enum mapping checked against the Profile
                and running on the legacy bridge; not verified over this
                repository's projection yet. Promote after the five acceptance
                steps in ``docs/service-adapter-contributing.md``.

Profile enums
    gear      : ``0`` auto / ``1`` low / ``2`` medium / ``3`` high /
                ``4`` silent / ``5`` turbo
    direction : ``1`` fixed / ``2`` horizontal / ``3`` vertical / ``4`` both

Bound entity domain
    The adapter accepts two kinds of Home Assistant entities:

      ``climate.`` -- air conditioner.  ``gear`` is reported from ``fan_mode``
                      and swing from ``swing_mode`` / ``swing_horizontal_mode``.
      ``fan.``     -- household fan / ceiling fan / ceiling fan light.  ``gear``
                      is derived from the HA ``percentage`` attribute and issued
                      through ``fan.set_percentage``; oscillation is mapped to
                      the ``direction`` characteristic via ``oscillating``.

Contract
    ``gear_values``        : for ``climate.`` entities only, maps HA ``fan_mode``
                             names to gear ints.  Unsupported names are skipped.
    ``gear_percent``       : for ``fan.`` entities only, maps a gear int to the
                             HA ``percentage`` it should be reported as / set to.
                             When omitted a default 1->25 / 2->50 / 3->100 is used.
    ``direction_enabled``  : controls whether the optional ``direction``
                             characteristic is read and reported.  Set it to false
                             for Profiles that expose only ``gear`` (most
                             household fans).

Details
    For ``climate.`` entities, swing is reported from Home Assistant's two swing
    attributes: newer releases expose ``swing_mode`` (vertical) and
    ``swing_horizontal_mode`` (horizontal), older releases only ``swing_mode``
    with ``on`` meaning vertical.

    For ``fan.`` entities swing is derived from ``oscillating``: ``True`` maps to
    ``direction=3`` (vertical) and ``False`` to ``direction=1`` (fixed).

    Unmapped gear or an unsupported swing name is skipped instead of forwarded,
    so the cloud never receives a value it would reject for the whole service.
    The on/off part of a household fan is normally bound separately through the
    ``switch`` adapter, which already accepts ``fan.`` domain entities.

Source
    This adapter is an extension of the ``fan`` adapter originally contributed
    to the ``ha-oneconnect-bridge`` project
    (https://github.com/xiasi0/ha-oneconnect-bridge), extended to additionally
    support ``fan.`` domain entities for household fans and ceiling fan lights.
"""

from __future__ import annotations

from ..models import ServiceRef

_CLIMATE_DEFAULT_GEAR_TO_HA = {
    0: "auto",
    1: "low",
    2: "medium",
    3: "high",
    4: "silent",
    5: "turbo",
}

# Default gear -> HA percentage mapping for household ``fan.`` entities when no
# ``gear_percent`` contract override is given.
_FAN_DEFAULT_GEAR_PERCENT = {
    1: 25,
    2: 50,
    3: 100,
}

_DIRECTION_TO_HA = {1: "off", 2: "horizontal", 3: "vertical", 4: "both"}
_HA_TO_DIRECTION = {name: direction for direction, name in _DIRECTION_TO_HA.items()}


class FanServiceAdapter:
    service_id = "fan"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "fan":
            raise ValueError("fan requires st: fan")
        unknown = set(dict(service.contract)) - {
            "gear_values",
            "gear_percent",
            "direction_enabled",
        }
        if unknown:
            raise ValueError(
                f"fan has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        _gear_values(service)
        _gear_percent(service)
        _direction_enabled(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("fan requires exactly one entity")
        domain = entity_ids[0].partition(".")[0]
        if domain not in {"climate", "fan"}:
            raise ValueError("fan requires a climate or fan entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None:
            return {}
        attributes = state.attributes
        domain = entity_ids[0].partition(".")[0]
        data: dict[str, object] = {}

        if domain == "climate":
            gear = _gear_values(service).get(attributes.get("fan_mode"))
        else:
            gear = _fan_gear_from_percentage(
                _gear_percent(service), attributes.get("percentage")
            )
        if gear is not None:
            data["gear"] = gear

        if not _direction_enabled(service):
            return data

        if domain == "climate":
            vertical = _active(attributes.get("swing_mode"))
            horizontal = _active(attributes.get("swing_horizontal_mode"))
            if vertical and horizontal:
                data["direction"] = 4
            elif vertical:
                data["direction"] = 3
            elif horizontal:
                data["direction"] = 2
            else:
                data["direction"] = 1
        else:
            data["direction"] = 3 if _active(attributes.get("oscillating")) else 1
        return data

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        attributes = state.attributes if state else {}
        domain = entity_ids[0].partition(".")[0]
        gear = _wire_int(payload.get("gear"))
        direction = _wire_int(payload.get("direction")) if _direction_enabled(service) else None

        if domain == "climate":
            gear_to_ha = {value: name for name, value in _gear_values(service).items()}
            fan_mode = gear_to_ha.get(gear) if gear is not None else None
            swing_mode = _DIRECTION_TO_HA.get(direction) if direction is not None else None
            if (gear is not None and fan_mode is None) or (
                direction is not None and swing_mode is None
            ):
                return
            if fan_mode is not None:
                if not _allowed(attributes.get("fan_modes"), fan_mode):
                    return
                await hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": entity_ids[0], "fan_mode": fan_mode},
                    blocking=True,
                )
            if swing_mode is not None:
                if not _allowed(attributes.get("swing_modes"), swing_mode):
                    return
                await hass.services.async_call(
                    "climate",
                    "set_swing_mode",
                    {"entity_id": entity_ids[0], "swing_mode": swing_mode},
                    blocking=True,
                )
            return

        # Home Assistant ``fan.`` entity (household fan / ceiling fan light).
        gear_to_percent = _gear_percent(service)
        percentage = gear_to_percent.get(gear) if gear is not None else None
        if gear is not None and percentage is None:
            return
        if percentage is not None:
            await hass.services.async_call(
                "fan",
                "set_percentage",
                {"entity_id": entity_ids[0], "percentage": percentage},
                blocking=True,
            )
        if direction is not None:
            osc = direction >= 2
            await hass.services.async_call(
                "fan",
                "oscillate",
                {"entity_id": entity_ids[0], "oscillating": osc},
                blocking=True,
            )


ADAPTER = FanServiceAdapter()


def _gear_values(service: ServiceRef) -> dict[str, int]:
    """Parse ``gear_values`` (climate: HA fan_mode name -> gear int)."""
    raw = service.contract_value(
        "gear_values",
        {name: value for value, name in _CLIMATE_DEFAULT_GEAR_TO_HA.items()},
    )
    if not isinstance(raw, dict) or not raw:
        raise ValueError("fan contract gear_values must be a non-empty mapping")
    values: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("fan contract gear_values must use non-empty text keys")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("fan contract gear_values must be integers")
        values[name] = value
    if len(set(values.values())) != len(values):
        raise ValueError("fan contract gear_values must be unique")
    return values


def _gear_percent(service: ServiceRef) -> dict[int, int]:
    """Parse ``gear_percent`` (household fan: gear int -> HA percentage)."""
    raw = service.contract_value("gear_percent", _FAN_DEFAULT_GEAR_PERCENT)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("fan contract gear_percent must be a non-empty mapping")
    values: dict[int, int] = {}
    for name, value in raw.items():
        try:
            gear = int(name)
        except (TypeError, ValueError):
            raise ValueError("fan contract gear_percent keys must be integer gears") from None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("fan contract gear_percent values must be integers")
        if not 0 <= value <= 100:
            raise ValueError("fan contract gear_percent values must be 0..100 percentages")
        values[gear] = value
    if len(set(values.values())) != len(values):
        raise ValueError("fan contract gear_percent values must be unique")
    return values


def _direction_enabled(service: ServiceRef) -> bool:
    value = service.contract_value("direction_enabled", True)
    if not isinstance(value, bool):
        raise ValueError("fan contract direction_enabled must be boolean")
    return value


def _fan_gear_from_percentage(
    gear_to_percent: dict[int, int], percentage: object
) -> int | None:
    """Map an HA ``fan.percentage`` (0..100) to the nearest configured gear."""
    if percentage is None or isinstance(percentage, bool):
        return None
    try:
        current = float(percentage)
    except (TypeError, ValueError):
        return None
    best_gear: int | None = None
    best_diff: float | None = None
    for gear, percent in gear_to_percent.items():
        diff = abs(current - percent)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_gear = gear
    return best_gear


def _active(value: object) -> bool:
    return value not in (None, "", "off", "none")


def _allowed(allowed: object, value: str) -> bool:
    """A command is skipped when the entity advertises a list that lacks it."""
    if isinstance(allowed, list) and allowed:
        return value in allowed
    return True


def _wire_int(value: object) -> int | None:
    """Accept only wire-plausible numbers (int or integral float); reject bool/str."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
