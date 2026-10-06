# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Download diagnostics: the settings, IFeel control's state, the last 30 minutes of polls, writes and events, a
snapshot of every unit-1 register (read when downloading; read only), and a summary of the IFeel debug logs. The
gateway address and the serial numbers are left out."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import ElectraConfigEntry

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ElectraConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    debug = coordinator.debug
    identity = coordinator.identity
    snapshot = await coordinator.async_slave1_snapshot()
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "status": asdict(data.raw_status or data.status) if data else None,
        "internal": asdict(data.internal) if data and data.internal else None,
        "ifeel": {
            "enabled": coordinator.ifeel.enabled,
            "status": coordinator.ifeel.status,
            **coordinator.ifeel.attributes(),
        },
        "identity": {  # the serial numbers are left out: only whether they were read
            "board_part": identity.board_part,
            "board_revision": identity.board_revision,
            "idu_product": identity.idu_product,
            "board_serial_read": identity.board_serial is not None,
            "idu_serial_read": identity.idu_serial is not None,
        } if identity else None,
        "debug_log": {
            "recording": debug.active,
            "current": asdict(debug.summary) if debug.summary else None,
            "recent": [asdict(summary) for summary in debug.history],
        },
        # the last 30 minutes: every poll with its raw registers, every write, connection events and IFeel events
        "history": [json.loads(line) for _, line in debug.ring],
        "slave1_snapshot": snapshot,
    }
