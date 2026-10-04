# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sensors: diagnostics, and the IFeel control status / source sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectraConfigEntry
from .coordinator import AcData
from .entity import ElectraEntity
from .ifeel import STATUSES
from .registers import (
    COMPRESSOR_STATES,
    IDU_FAULTS,
    IFEEL_SOURCE_MODBUS,
    IFEEL_SOURCE_REMOTE,
    ODU_FAULTS,
    fault_state,
)

IFEEL_SOURCES = ["off", "home_assistant", "remote", "unknown", "on"]

COMPRESSOR_NOTE = (
    "Reliable in Cool and Heat. In Dry the running state can be reported while the compressor draws little power; in "
    "Fan it is never set. 'Stopped less than 8 min ago' never shows in Heat: the unit has no 8-minute timer there. "
    "Based on the unit's internal status cells."
)
BOARD_ROOM_NOTE = (
    "The unit's own sensor, inside the indoor unit. While heating it measures the unit's own warm air, not the room."
)


@dataclass(frozen=True, kw_only=True)
class ElectraSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AcData], Any]
    attrs_fn: Callable[[AcData], dict[str, Any]] | None = None
    needs_internal: bool = False
    available_fn: Callable[[AcData], bool] | None = None  # extra condition, e.g. a plausible value


SENSORS: tuple[ElectraSensorDescription, ...] = (
    ElectraSensorDescription(
        key="compressor_state",
        translation_key="compressor_state",
        device_class=SensorDeviceClass.ENUM,
        options=COMPRESSOR_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        needs_internal=True,
        value_fn=lambda d: d.internal.compressor_state if d.internal else None,
        attrs_fn=lambda d: {"note": COMPRESSOR_NOTE},
    ),
    ElectraSensorDescription(
        key="coil_temperature",
        translation_key="coil_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=0,  # whole degrees
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        needs_internal=True,
        value_fn=lambda d: d.internal.coil_temp if d.internal else None,
        available_fn=lambda d: d.internal is not None and d.internal.coil_temp is not None,
    ),
    # The keys stay "..._fault_code" so that entities created by the first version keep their unique ids.
    ElectraSensorDescription(
        key="idu_fault_code",
        translation_key="idu_fault",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: fault_state(IDU_FAULTS, d.status.idu_fault),
        attrs_fn=lambda d: {"code": d.status.idu_fault},
    ),
    ElectraSensorDescription(
        key="board_room_temperature",
        translation_key="board_room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=0,  # whole degrees
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.raw_status or d.status).room_temp,  # 0x3303, the board's own sensor
        attrs_fn=lambda d: {"note": BOARD_ROOM_NOTE},
    ),
    ElectraSensorDescription(
        key="ifeel_source",
        translation_key="ifeel_source",
        device_class=SensorDeviceClass.ENUM,
        options=IFEEL_SOURCES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: ifeel_source(d),
        attrs_fn=lambda d: {"source_known": d.internal is not None},
    ),
    ElectraSensorDescription(
        key="remote_ifeel_temperature",
        translation_key="remote_ifeel_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=0,  # whole degrees
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        needs_internal=True,
        value_fn=lambda d: d.internal.mirror if d.internal else None,
        available_fn=lambda d: ifeel_source(d) == "remote",
    ),
    ElectraSensorDescription(
        key="odu_fault_code",
        translation_key="odu_fault",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: fault_state(ODU_FAULTS, d.status.odu_fault),
        # The ODU code table is only loosely matched to the service manuals.
        attrs_fn=lambda d: {"code": d.status.odu_fault, "description_confidence": "best effort"},
    ),
)


def ifeel_source(d: AcData) -> str:
    """Who drives IFeel (decided): from 0x4801 bit 14 and 0x4803; without usable slave-1 cells only on / off from
    0x3306, which can read on while IFeel is off inside the unit."""
    if d.internal is None:
        return "on" if (d.raw_status or d.status).ifeel else "off"
    if not d.internal.ifeel_active:
        return "off"
    if d.internal.ifeel_source == IFEEL_SOURCE_MODBUS:
        return "home_assistant"
    if d.internal.ifeel_source == IFEEL_SOURCE_REMOTE:
        return "remote"
    return "unknown"


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [ElectraSensor(coordinator, entry.entry_id, entry.title, d) for d in SENSORS]
    entities.append(IFeelStatusSensor(coordinator, entry.entry_id, entry.title, "ifeel_status"))
    async_add_entities(entities)


class ElectraSensor(ElectraEntity, SensorEntity):
    entity_description: ElectraSensorDescription

    def __init__(self, coordinator, entry_id: str, name: str, description: ElectraSensorDescription) -> None:
        super().__init__(coordinator, entry_id, name, description.key)
        self.entity_description = description
        self._needs_internal = description.needs_internal

    @property
    def available(self) -> bool:
        fn = self.entity_description.available_fn
        return super().available and (fn is None or fn(self.coordinator.data))

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


class IFeelStatusSensor(ElectraEntity, SensorEntity):
    """What IFeel control is doing right now (docs/IFEEL_DESIGN.md, "Entities for IFeel control")."""

    _attr_translation_key = "ifeel_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES

    @property
    def native_value(self) -> str:
        return self.coordinator.ifeel.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.ifeel.attributes()
