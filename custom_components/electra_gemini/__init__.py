# Electra GEMINI AC (Modbus): a Home Assistant integration for Electra/Airwell GEMINI air conditioners
# Copyright (C) 2026 shaysudm
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
#
# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Electra/Airwell GEMINI AC over Modbus (Elfin EW11)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_SCAN_INTERVAL, CONTROL_UNIT_ID, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import ElectraCoordinator
from .modbus import ModbusTcpClient

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.SELECT]

type ElectraConfigEntry = ConfigEntry[ElectraCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ElectraConfigEntry) -> bool:
    # The unit ID is fixed; an entry created by an earlier version may still store one, it is ignored.
    client = ModbusTcpClient(entry.data[CONF_HOST], entry.data[CONF_PORT], write_unit=CONTROL_UNIT_ID)
    coordinator = ElectraCoordinator(
        hass,
        entry,
        client,
        CONTROL_UNIT_ID,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    # Raises ConfigEntryNotReady if the first poll fails, Home Assistant then retries with back-off.
    coordinator.connection = (entry.data[CONF_HOST], entry.data[CONF_PORT])  # to tell a connection change on reload
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_read_identity()  # for the device info: before the entities are added
    entry.runtime_data = coordinator
    _remove_replaced_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_setup_ifeel()

    async def _on_stop(event: Event) -> None:
        await coordinator.async_handback()  # rule 10: hand back when Home Assistant stops cleanly

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    entry.async_on_unload(entry.add_update_listener(_async_options_changed))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElectraConfigEntry) -> bool:
    coordinator = entry.runtime_data
    await coordinator.async_handback()  # skipped for a reload caused by an options change
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await coordinator.async_shutdown()
    return unloaded


async def _async_options_changed(hass: HomeAssistant, entry: ElectraConfigEntry) -> None:
    # Rule 10: an options change reloads without handing IFeel back; the reloaded integration takes over.
    # A changed connection (host / port, also set in the options) hands back first, through the old connection.
    coordinator = entry.runtime_data
    coordinator.keep_ifeel_on_unload = coordinator.connection == (entry.data[CONF_HOST], entry.data[CONF_PORT])
    await hass.config_entries.async_reload(entry.entry_id)


def _remove_replaced_entities(hass: HomeAssistant, entry: ElectraConfigEntry) -> None:
    """The "IFeel mode" binary sensor (0x3306, which can read on while IFeel is off) is replaced by "IFeel source"."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_ifeel_mode")
    if entity_id:
        registry.async_remove(entity_id)
