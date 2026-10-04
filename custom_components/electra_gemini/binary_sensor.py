# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostic binary sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectraConfigEntry
from .coordinator import AcData
from .entity import ElectraEntity
from .registers import IDU_FAULTS, ODU_FAULTS, fault_text
from .sensor import COMPRESSOR_NOTE


@dataclass(frozen=True, kw_only=True)
class ElectraBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[AcData], bool]
    attrs_fn: Callable[[AcData], dict[str, Any]] | None = None
    needs_internal: bool = False


BINARY_SENSORS: tuple[ElectraBinaryDescription, ...] = (
    ElectraBinaryDescription(
        key="compressor_running",
        translation_key="compressor_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        needs_internal=True,
        is_on_fn=lambda d: d.internal is not None and d.internal.running,
        attrs_fn=lambda d: {"note": COMPRESSOR_NOTE},
    ),
    ElectraBinaryDescription(
        key="idu_fault",
        translation_key="idu_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.status.idu_fault != 0,
        attrs_fn=lambda d: {"code": d.status.idu_fault, "description": fault_text(IDU_FAULTS, d.status.idu_fault)},
    ),
    ElectraBinaryDescription(
        key="odu_fault",
        translation_key="odu_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.status.odu_fault != 0,
        attrs_fn=lambda d: {
            "code": d.status.odu_fault,
            "description": fault_text(ODU_FAULTS, d.status.odu_fault),
            "description_confidence": "best effort",
        },
    ),
    ElectraBinaryDescription(
        key="alarm",
        translation_key="alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.status.alarm,
    ),
    ElectraBinaryDescription(
        key="overflow",
        translation_key="overflow",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.status.overflow,
    ),
    ElectraBinaryDescription(
        key="defrost",
        translation_key="defrost",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.status.defrost,
    ),
    ElectraBinaryDescription(
        key="timer_active",
        translation_key="timer_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.status.timer,  # 0x330E: a timer is set on the remote (timer 1 and 2 share it)
    ),
    # "IFeel mode" (0x3306) was replaced by the "IFeel source" sensor: 0x3306 can read on while IFeel is off
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ElectraBinarySensor(coordinator, entry.entry_id, entry.title, d) for d in BINARY_SENSORS)


class ElectraBinarySensor(ElectraEntity, BinarySensorEntity):
    entity_description: ElectraBinaryDescription

    def __init__(self, coordinator, entry_id: str, name: str, description: ElectraBinaryDescription) -> None:
        super().__init__(coordinator, entry_id, name, description.key)
        self.entity_description = description
        self._needs_internal = description.needs_internal

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None
