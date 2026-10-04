# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Constants for the Electra GEMINI AC (Modbus) integration."""
from __future__ import annotations

DOMAIN = "electra_gemini"

CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_NAME = "AC"
DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 10  # seconds
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 300

CONTROL_UNIT_ID = 160  # 0xA0: control and status registers; fixed, not configurable
INTERNAL_UNIT_ID = 1  # the "slave 1" unit with the internal status cells (read only)

MIN_WRITE_INTERVAL = 2.0  # seconds between two writes to the AC

# After a write the unit needs a moment before its registers show the new value (0.5 s or more was measured for the
# flags): wait, read, and read again after each of these delays until the value read back is the one written.
# The Shabbat flag register follows a write only after about 3.6 s, hence the longer tail (about 7.5 s at most).
READBACK_DELAYS = (0.6, 0.6, 0.8, 1.0, 1.0, 1.5, 2.0)  # seconds

# Unit 1 (the internal cells) is optional: if it fails this many polls in a row, it is only tried again every
# INTERNAL_RETRY_INTERVAL seconds, so that a unit without it is not slowed down by a timeout on every poll.
INTERNAL_MAX_FAILURES = 3
INTERNAL_RETRY_INTERVAL = 300  # seconds

# --- IFeel control options (IFEEL_DESIGN.md) -------------------------------------------------------------------------
CONF_IFEEL_SENSORS = "ifeel_sensors"  # the temperature sensors offered in the "IFeel room sensor" select
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_KEEPALIVE = "keepalive"
CONF_MIN_OFF = "min_off"  # Cool: "timer" (the unit's 8-minute timer, recommended) or "3".."8" minutes
CONF_HEAT_MIN_OFF = "heat_min_off"  # Heat: "3".."8" minutes (no 8-minute timer in Heat)
CONF_STALE_MINUTES = "stale_minutes"
CONF_COIL_LIMIT = "coil_limit"  # Cool
CONF_HEAT_COIL_LIMIT = "heat_coil_limit"  # Heat
CONF_MAX_RUN = "max_run"
CONF_ALLOW_NO_COIL = "allow_without_coil_protection"
CONF_OVERRIDE_MAX_RUN = "override_max_run"
CONF_REMOTE_OFF_ON_KEEPS = "remote_off_on_keeps"  # the remote turning the AC off / on pauses IFeel control
CONF_SHABBAT_PAUSES = "shabbat_pauses"  # Shabbat mode pauses IFeel control
SECTION_IFEEL = "ifeel"
SECTION_UNSAFE = "unsafe"
MIN_OFF_TIMER = "timer"

IFEEL_DEFAULTS = {
    CONF_IFEEL_SENSORS: [],
    CONF_COLD_TOLERANCE: 0.5,
    CONF_HOT_TOLERANCE: 0.5,
    CONF_KEEPALIVE: 180,
    CONF_MIN_OFF: MIN_OFF_TIMER,
    CONF_HEAT_MIN_OFF: "5",
    CONF_STALE_MINUTES: 30,
    CONF_COIL_LIMIT: 5,
    CONF_HEAT_COIL_LIMIT: 65,
    CONF_MAX_RUN: 45,
    CONF_REMOTE_OFF_ON_KEEPS: False,
    CONF_SHABBAT_PAUSES: False,
    CONF_ALLOW_NO_COIL: False,
    CONF_OVERRIDE_MAX_RUN: 10,
}

MANUFACTURER = "Electra/Airwell"
MODEL = "GEMINI IDU"
