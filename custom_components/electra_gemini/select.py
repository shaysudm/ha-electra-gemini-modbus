# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "IFeel room sensor" select (docs/IFEEL_DESIGN.md, rule 12): which of the sensors offered in the options IFeel control
uses. An automation or a dashboard can change it; the choice is saved and restored by the coordinator."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectraConfigEntry
from .entity import ElectraEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([IFeelRoomSensorSelect(entry.runtime_data, entry.entry_id, entry.title, "ifeel_room_sensor")])


class IFeelRoomSensorSelect(ElectraEntity, SelectEntity):
    _attr_translation_key = "ifeel_room_sensor"

    def _names(self) -> dict[str, str]:
        """Option label -> entity id (the sensor's name; the entity id where names repeat or a sensor is missing)."""
        labels: dict[str, str] = {}
        for entity_id in self.coordinator.ifeel_sensors:
            state = self.hass.states.get(entity_id)
            name = state.name if state is not None else entity_id
            if name in labels:
                name = f"{name} ({entity_id})"
            labels[name] = entity_id
        return labels

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.ifeel_sensors)

    @property
    def options(self) -> list[str]:
        return list(self._names())

    @property
    def current_option(self) -> str | None:
        for label, entity_id in self._names().items():
            if entity_id == self.coordinator.room_sensor:
                return label
        return None

    @property
    def extra_state_attributes(self) -> dict:
        return {"entity_id": self.coordinator.room_sensor}

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_room_sensor(self._names()[option])
