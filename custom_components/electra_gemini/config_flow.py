# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config flow and options flow."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import ElectraConfigEntry
from .const import (
    CONF_ALLOW_NO_COIL,
    CONF_COIL_LIMIT,
    CONF_COLD_TOLERANCE,
    CONF_HEAT_COIL_LIMIT,
    CONF_HEAT_MIN_OFF,
    CONF_HOT_TOLERANCE,
    CONF_IFEEL_SENSORS,
    CONF_KEEPALIVE,
    CONF_MAX_RUN,
    CONF_MIN_OFF,
    CONF_OVERRIDE_MAX_RUN,
    CONF_REMOTE_OFF_ON_KEEPS,
    CONF_SCAN_INTERVAL,
    CONF_SHABBAT_PAUSES,
    CONF_STALE_MINUTES,
    IFEEL_DEFAULTS,
    INTERNAL_UNIT_ID,
    MIN_OFF_TIMER,
    SECTION_IFEEL,
    SECTION_UNSAFE,
    CONTROL_UNIT_ID,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .modbus import ModbusError, ModbusTcpClient
from .registers import INTERNAL_ADDRESS, INTERNAL_COUNT, STATUS_ADDRESS, STATUS_COUNT, parse_internal, parse_status

# A read can come back as all 0xFFFF while another client is connected to the EW11: try a few times.
VALIDATE_TRIES = 3
VALIDATE_RETRY_DELAY = 1.0  # seconds


def _unique_id(host: str, port: int) -> str:
    # Same format as before the unit ID was removed from the form, so existing entries are still recognised.
    return f"{host}:{port}:{CONTROL_UNIT_ID}"


def _sensors_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True))


async def _async_validate(host: str, port: int, *, check_slave1: bool = False) -> bool:
    """Connect and read the status block; raises ModbusError / ValueError. With check_slave1, also read the internal
    cells (unit 1) and return whether they are usable (IFeel control needs them)."""
    client = ModbusTcpClient(host, port, write_unit=CONTROL_UNIT_ID)
    try:
        for attempt in range(VALIDATE_TRIES):
            try:
                regs = await client.read_holding_registers(CONTROL_UNIT_ID, STATUS_ADDRESS, STATUS_COUNT)
                if all(value == 0xFFFF for value in regs):
                    raise ValueError("all 0xFFFF (a failed read)")
                status = parse_status(regs)
                break
            except ValueError:
                if attempt == VALIDATE_TRIES - 1:
                    raise
                await asyncio.sleep(VALIDATE_RETRY_DELAY)
        if not check_slave1:
            return True
        for attempt in range(VALIDATE_TRIES):
            try:
                regs = await client.read_holding_registers(INTERNAL_UNIT_ID, INTERNAL_ADDRESS, INTERNAL_COUNT)
                if all(value == 0xFFFF for value in regs):
                    raise ValueError("all 0xFFFF (a failed read)")
                parse_internal(regs, status.room_temp, status.mode)
                return True
            except (ModbusError, ValueError):
                if attempt < VALIDATE_TRIES - 1:
                    await asyncio.sleep(VALIDATE_RETRY_DELAY)
        return False
    finally:
        await client.close()


class ElectraConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._title = DEFAULT_NAME
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(_unique_id(host, user_input[CONF_PORT]))
            self._abort_if_unique_id_configured()
            try:
                slave1_usable = await _async_validate(host, user_input[CONF_PORT], check_slave1=True)
            except ModbusError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "invalid_response"
            else:
                self._title = user_input[CONF_NAME]
                self._data = {CONF_HOST: host, CONF_PORT: user_input[CONF_PORT]}
                return await (self.async_step_sensors() if slave1_usable else self.async_step_no_slave1())
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,  # deliberately no default
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the connection (EW11 address and port): checked first; the update listener then hands IFeel back
        through the old connection and reloads."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            if (host, port) == (entry.data[CONF_HOST], entry.data[CONF_PORT]):
                return self.async_abort(reason="reconfigure_successful")
            unique_id = _unique_id(host, port)
            if any(e.unique_id == unique_id for e in self._async_current_entries() if e.entry_id != entry.entry_id):
                errors["base"] = "already_configured"
            else:
                try:
                    await _async_validate(host, port)
                except ModbusError:
                    errors["base"] = "cannot_connect"
                except ValueError:
                    errors["base"] = "invalid_response"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port}, unique_id=unique_id
                    )
                    return self.async_abort(reason="reconfigure_successful")
        schema = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PORT): int})
        schema = self.add_suggested_values_to_schema(schema, user_input or entry.data)
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The internal cells are usable: choose the room sensors offered for IFeel control (can be left empty)."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._title,
                data=self._data,
                options={CONF_IFEEL_SENSORS: user_input.get(CONF_IFEEL_SENSORS, [])},
            )
        schema = vol.Schema({vol.Optional(CONF_IFEEL_SENSORS, default=[]): _sensors_selector()})
        return self.async_show_form(step_id="sensors", data_schema=schema)

    async def async_step_no_slave1(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The internal cells cannot be read: a warning (IFeel control not available), then create the entry."""
        if user_input is not None:
            return self.async_create_entry(title=self._title, data=self._data)
        return self.async_show_form(step_id="no_slave1", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ElectraConfigEntry) -> OptionsFlow:
        return ElectraOptionsFlow()


class ElectraOptionsFlow(OptionsFlow):
    """Polling interval, and the IFeel control settings (docs/IFEEL_DESIGN.md). Stored flat in the entry's options. The
    connection is changed with "Reconfigure" (async_step_reconfigure)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            for section_key in (SECTION_IFEEL, SECTION_UNSAFE):
                data.update(user_input.get(section_key, {}))
            for key in (
                CONF_KEEPALIVE, CONF_STALE_MINUTES, CONF_COIL_LIMIT, CONF_HEAT_COIL_LIMIT, CONF_MAX_RUN,
                CONF_OVERRIDE_MAX_RUN,
            ):  # fmt: skip
                data[key] = int(data[key])
            return self.async_create_entry(data=data)

        options = self.config_entry.options
        o = {key: options.get(key, default) for key, default in IFEEL_DEFAULTS.items()}

        def number(lo: float, hi: float, step: float, unit: str) -> NumberSelector:
            return NumberSelector(
                NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX)
            )

        ifeel_schema = vol.Schema(
            {
                vol.Optional(CONF_IFEEL_SENSORS, default=o[CONF_IFEEL_SENSORS]): _sensors_selector(),
                vol.Required(CONF_COLD_TOLERANCE, default=o[CONF_COLD_TOLERANCE]): number(0.1, 3, 0.1, "°C"),
                vol.Required(CONF_HOT_TOLERANCE, default=o[CONF_HOT_TOLERANCE]): number(0.1, 3, 0.1, "°C"),
                vol.Required(CONF_KEEPALIVE, default=o[CONF_KEEPALIVE]): number(60, 300, 10, "s"),
                vol.Required(CONF_MIN_OFF, default=str(o[CONF_MIN_OFF])): SelectSelector(
                    SelectSelectorConfig(
                        options=[MIN_OFF_TIMER, "3", "4", "5", "6", "7", "8"],
                        translation_key="min_off",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_HEAT_MIN_OFF, default=str(o[CONF_HEAT_MIN_OFF])): SelectSelector(
                    SelectSelectorConfig(
                        options=["3", "4", "5", "6", "7", "8"],
                        translation_key="min_off",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_STALE_MINUTES, default=o[CONF_STALE_MINUTES]): number(5, 120, 1, "min"),
                vol.Required(CONF_COIL_LIMIT, default=o[CONF_COIL_LIMIT]): number(3, 10, 1, "°C"),
                vol.Required(CONF_HEAT_COIL_LIMIT, default=o[CONF_HEAT_COIL_LIMIT]): number(60, 75, 1, "°C"),
                vol.Required(CONF_MAX_RUN, default=o[CONF_MAX_RUN]): number(15, 120, 1, "min"),
                vol.Required(CONF_REMOTE_OFF_ON_KEEPS, default=o[CONF_REMOTE_OFF_ON_KEEPS]): BooleanSelector(),
                vol.Required(CONF_SHABBAT_PAUSES, default=o[CONF_SHABBAT_PAUSES]): BooleanSelector(),
            }
        )
        unsafe_schema = vol.Schema(
            {
                vol.Required(CONF_ALLOW_NO_COIL, default=o[CONF_ALLOW_NO_COIL]): BooleanSelector(),
                vol.Required(CONF_OVERRIDE_MAX_RUN, default=o[CONF_OVERRIDE_MAX_RUN]): number(5, 30, 1, "min"),
            }
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): number(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, 1, "s"),
                vol.Required(SECTION_IFEEL): section(ifeel_schema, {"collapsed": False}),
                vol.Required(SECTION_UNSAFE): section(unsafe_schema, {"collapsed": True}),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
