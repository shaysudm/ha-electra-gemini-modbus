# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Base entity."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ElectraCoordinator
from .registers import Identity


def _identity_info(identity: Identity | None) -> dict[str, str]:
    """The device is the controller board (GEMINI IDU): its part number, revision and barcode; the indoor unit's
    product and serial numbers go with the hardware version (owner, 2026-10-04: Home Assistant has one serial field)."""
    if identity is None:
        return {}
    info: dict[str, str] = {}
    if identity.board_part:
        info["model_id"] = identity.board_part
    if identity.board_serial:
        info["serial_number"] = identity.board_serial
    idu = ", ".join(
        part for part in (identity.idu_product, identity.idu_serial and f"S/N {identity.idu_serial}") if part
    )
    if identity.board_revision or idu:
        hw = identity.board_revision or ""
        if idu:
            hw = f"{hw} (indoor unit {idu})".strip()
        info["hw_version"] = hw
    return info


class ElectraEntity(CoordinatorEntity[ElectraCoordinator]):
    _attr_has_entity_name = True
    # True for entities that come from the internal (unit 1) cells: unavailable when that read fails.
    _needs_internal = False

    def __init__(self, coordinator: ElectraCoordinator, entry_id: str, name: str, key: str | None) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{key}" if key else entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            **_identity_info(coordinator.identity),
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self._needs_internal or self.coordinator.data.internal is not None
