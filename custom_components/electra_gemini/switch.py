# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Control switches: Shabbat mode (0x330B), sleep mode (0x3312) and IFeel control.

Written as 0x0A (on) or 0 (off), the only values the unit accepts (1 is acknowledged but ignored). What they change
in the AC's behaviour was not seen in the one-minute tests (docs/REGISTER_MAP.md, "Shabat, timer and sleep flags").
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectraConfigEntry
from .coordinator import AcData
from .entity import ElectraEntity


@dataclass(frozen=True, kw_only=True)
class ElectraSwitchDescription(SwitchEntityDescription):
    is_on_fn: Callable[[AcData], bool]
    request_key: str  # the keyword of ElectraCoordinator.async_set that writes it


SWITCHES: tuple[ElectraSwitchDescription, ...] = (
    ElectraSwitchDescription(
        key="shabat_mode",
        translation_key="shabat_mode",
        is_on_fn=lambda d: d.status.shabat,
        request_key="shabat",
    ),
    ElectraSwitchDescription(
        key="sleep_mode",
        translation_key="sleep_mode",
        is_on_fn=lambda d: d.status.sleep,
        request_key="sleep",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [ElectraSwitch(coordinator, entry.entry_id, entry.title, d) for d in SWITCHES]
    entities.append(IFeelControlSwitch(coordinator, entry.entry_id, entry.title, "ifeel_control"))
    async_add_entities(entities)


class ElectraSwitch(ElectraEntity, SwitchEntity):
    """The state is always what the unit reports (read back after each write), never assumed."""

    entity_description: ElectraSwitchDescription

    def __init__(self, coordinator, entry_id: str, name: str, description: ElectraSwitchDescription) -> None:
        super().__init__(coordinator, entry_id, name, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._request("turn_on")
        await self.coordinator.async_set(**{self.entity_description.request_key: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._request("turn_off")
        await self.coordinator.async_set(**{self.entity_description.request_key: False})


class IFeelControlSwitch(ElectraEntity, SwitchEntity):
    """On = the chosen room sensor drives the AC through IFeel (docs/IFEEL_DESIGN.md). It stays on while control is
    suspended or held back and goes off when the user or a stop (failed sensor, Shabbat, remote mode change, ...) turns
    it off; the "IFeel control status" sensor says why. Restored after a restart by the coordinator."""

    _attr_translation_key = "ifeel_control"

    @property
    def is_on(self) -> bool:
        return self.coordinator.ifeel.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self.coordinator.ifeel.status}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._request("turn_on")
        await self.coordinator.async_ifeel_enable()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._request("turn_off")
        await self.coordinator.async_ifeel_disable()
