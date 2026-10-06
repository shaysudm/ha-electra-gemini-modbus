# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Climate entity."""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectraConfigEntry
from .const import DOMAIN
from .entity import ElectraEntity
from .registers import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    MAX_SETPOINT,
    MIN_SETPOINT,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    MODE_OFF,
)

# Auto is not offered (owner decision 2026-10-04: on this unit it can freeze the indoor coil); it is only shown while the
# unit is in it (set at the remote). Fan: only what the remote offers; Turbo (4) and Very Low (5) are stored by the unit
# but do nothing (tested 2026-10-01), so they are not offered.
MODE_TO_HVAC = {
    MODE_OFF: HVACMode.OFF,
    MODE_COOL: HVACMode.COOL,
    MODE_HEAT: HVACMode.HEAT,
    MODE_AUTO: HVACMode.AUTO,
    MODE_DRY: HVACMode.DRY,
    MODE_FAN: HVACMode.FAN_ONLY,
}
HVAC_TO_MODE = {hvac: mode for mode, hvac in MODE_TO_HVAC.items()}
OFFERED_HVAC_MODES = [hvac for mode, hvac in MODE_TO_HVAC.items() if mode != MODE_AUTO]
AUTO_NOTE = (
    "Set at the remote. Auto is not supported from Home Assistant: on this unit it can freeze the indoor coil. "
    "'Heating' is the unit's own choice (bit 11) and was also shown during the frozen-coil run."
)

FAN_TO_NAME = {
    FAN_LOW: "low",
    FAN_MEDIUM: "medium",
    FAN_HIGH: "high",
    FAN_AUTO: "auto",
}
NAME_TO_FAN = {name: fan for fan, name in FAN_TO_NAME.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ElectraClimate(entry.runtime_data, entry.entry_id, entry.title)])


class ElectraClimate(ElectraEntity, ClimateEntity):
    _attr_name = None
    _attr_translation_key = "ac"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_SETPOINT
    _attr_max_temp = MAX_SETPOINT
    _attr_fan_modes = list(FAN_TO_NAME.values())
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator, entry_id: str, name: str) -> None:
        super().__init__(coordinator, entry_id, name, None)

    @property
    def _status(self):
        return self.coordinator.data.status

    @property
    def precision(self) -> float:
        # the board's sensor is whole degrees; the room sensor used by IFeel control is finer
        return PRECISION_TENTHS if self.coordinator.ifeel.controlling else PRECISION_WHOLE

    @property
    def target_temperature_step(self) -> float:
        return 1

    @property
    def current_temperature(self) -> float | None:
        """The chosen room sensor while IFeel control drives the unit (decided), else the board's own sensor."""
        temp = self.coordinator.ifeel.attributes()["sensor_value"]
        if self.coordinator.ifeel.controlling and temp is not None:
            return round(temp, 1)
        return self._status.room_temp

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Auto only while the unit is in it (set at the remote): Home Assistant needs the current mode in the list."""
        if self._status.mode == MODE_AUTO:
            return [*OFFERED_HVAC_MODES, HVACMode.AUTO]
        return OFFERED_HVAC_MODES

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        controlling = self.coordinator.ifeel.controlling
        attrs: dict[str, Any] = {"current_temperature_source": "room_sensor" if controlling else "board"}
        if self._status.mode == MODE_AUTO:
            internal = self.coordinator.data.internal
            attrs["auto_direction"] = None if internal is None else ("heating" if internal.heat_side else "cooling")
            attrs["note"] = AUTO_NOTE
        return attrs

    @property
    def target_temperature(self) -> float | None:
        return self._status.setpoint

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self._status.mode
        return None if mode is None else MODE_TO_HVAC[mode]

    @property
    def fan_mode(self) -> str | None:
        return FAN_TO_NAME.get(self._status.fan)

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self._status.mode
        if mode is None:
            return None
        if mode == MODE_OFF:
            return HVACAction.OFF
        if mode == MODE_FAN:
            return HVACAction.FAN
        internal = self.coordinator.data.internal
        if internal is None:
            return None
        if mode == MODE_DRY:
            # The running bit can be set in Dry while the compressor draws little power; the mode itself means drying.
            return HVACAction.DRYING
        if not internal.running:
            return HVACAction.IDLE
        if mode == MODE_HEAT:
            return HVACAction.HEATING
        if mode == MODE_AUTO:
            # set at the remote: the direction Auto chose, 0x4805 bit 11 (the indoor side's choice, not proof of heating)
            return HVACAction.HEATING if internal.heat_side else HVACAction.COOLING
        return HVACAction.COOLING

    def _refuse_auto(self, hvac_mode: HVACMode | None) -> None:
        if hvac_mode == HVACMode.AUTO:
            self._refused("hvac_mode auto", "auto_not_supported")
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="auto_not_supported")

    def _refuse_in_auto(self) -> None:
        """Set at the remote: no setpoint or fan changes in Auto (they would write Auto again; owner, 2026-10-04)."""
        if self._status.mode == MODE_AUTO:
            self._refused("setpoint / fan in Auto", "in_auto")
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="in_auto")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._request("set_hvac_mode", hvac_mode=hvac_mode)
        self._refuse_auto(hvac_mode)
        await self.coordinator.async_set(mode=HVAC_TO_MODE[hvac_mode])

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get("hvac_mode")
        if temperature is None and hvac_mode is None:
            return
        self._request("set_temperature", temperature=temperature, hvac_mode=hvac_mode)
        self._refuse_auto(hvac_mode)
        if hvac_mode is None:
            self._refuse_in_auto()
        await self.coordinator.async_set(
            mode=HVAC_TO_MODE[hvac_mode] if hvac_mode is not None else None,
            setpoint=None if temperature is None else round(temperature),
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._request("set_fan_mode", fan_mode=fan_mode)
        self._refuse_in_auto()
        await self.coordinator.async_set(fan=NAME_TO_FAN[fan_mode])

    async def async_turn_on(self) -> None:
        self._request("turn_on")
        await self.coordinator.async_set(mode=self.coordinator.last_active_mode)

    async def async_turn_off(self) -> None:
        self._request("turn_off")
        await self.coordinator.async_set(mode=MODE_OFF)
