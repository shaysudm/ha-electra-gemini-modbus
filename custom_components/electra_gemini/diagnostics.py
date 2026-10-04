# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Download diagnostics: the settings, IFeel control's state and a summary of the recent IFeel debug logs."""
from __future__ import annotations

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
        "debug_log": {
            "recording": debug.active,
            "current": asdict(debug.summary) if debug.summary else None,
            "recent": [asdict(summary) for summary in debug.history],
        },
    }
