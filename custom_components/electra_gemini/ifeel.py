# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""IFeel control: drive the unit from a Home Assistant room sensor through its IFeel input. No Home Assistant imports.

The design is in docs/IFEEL_DESIGN.md; "rule N" below refers to its numbered control rules.

The controller is a state machine. It is fed with one Observation per poll (and one after each settings write made by
the integration), with the room sensor's readings and with the user's requests, and it returns the writes to make
(Write) and events for the debug log and notifications (Event). It never talks to the unit itself, and everything it
asks for is on the client's allow-list (modbus.check_write).

In short: a thermostat like Home Assistant's generic thermostat decides "on" or "off" from the room sensor and two
tolerances and tells the unit through IFeel values (Cool: setpoint + 1 = on, setpoint - 1 = off; Heat the reverse;
no IFeel in Fan and Dry; Auto is not supported at all). IFeel is enabled with one block [1, value] and kept alive
with 0x3306 = 1. Every switch-off leaves a value behind that cannot start the compressor (16 after cooling, 30 after
heating).

The unit keeps a Modbus IFeel value only while slave 160 is read in blocks at least about every 2 s (found 2026-10-02):
while needs_fast_reads is true the coordinator reads the status block every second and passes each read in as a
non-fresh Observation. A value lost anyway is re-sent at once, once more if lost again within 30 s (Cool), then IFeel
control stops (docs/IFEEL_DESIGN.md, "Fast status reads and lost values").
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .registers import (
    IFEEL_SOURCE_MODBUS,
    IFEEL_SOURCE_REMOTE,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    MODE_OFF,
    REG_IFEEL,
    REG_IFEEL_TEMP,
    Internal,
    Status,
)

# --- timings and limits (docs/IFEEL_DESIGN.md) ------------------------------------------------------------------------
NEW_VALUE_INTERVAL = 180.0  # rule 2: a new value at most every 180 s (fixed)
ENABLE_REPEAT_DELAY = 1.5  # rule 1: the value is written again 1.5 s after the enable block
VERIFY_GRACE = 3.0  # no verification in the first 3 s after a value write (the enable's old value)
RECOVERY_HOLD = 30.0  # rule 4: a recovery only counts if the value still holds 30 s later
EXPIRY_AFTER = 400.0  # rule 5: IFeel found off this long after the last enable / keep-alive = an expiry, not remote use
SENSOR_GRACE = 120.0  # rule 7: unavailable for longer than this -> hand back
SENSOR_MIN, SENSOR_MAX = 10.0, 40.0  # rule 7
COIL_FOR = 60.0  # rule 8: coil at or below the limit this long while running -> guard
COIL_RELEASE = 10  # rule 8: guard released when the coil reads above this
HEAT_COIL_RELEASE = 50  # Heat coil guard (decided 2026-10-03): released when the coil reads at or below this
FAST_READ_INTERVAL = 1.0  # status block (0x3300 x 19) every second while IFeel is on (the unit drops the value otherwise)
FAST_MISMATCHES = 2  # fast reads in a row with 0x3307 not the value written -> check the slave-1 cells at once
TIMER_SECONDS = 480.0  # the unit's 8-minute timer (0x4805 bit 10)
NO_SLAVE1_RUN_MARGIN = 210.0  # without slave 1: the compressor may run this long after a stop value (min run + margin)
SAFE_COOLING, SAFE_HEATING = 16, 30  # rule 1: left behind before every switch-off
CELLS_MAX_AGE = 30.0  # slave-1 cells older than this are not used for decisions
OWN_SETTINGS_WINDOW = 30.0  # a settings change that matches the integration's own write within this time is its own
SHABBAT_CLEANUP_TRIES = 5
COIL_FAULT_CODES = (1, 3)  # IDU fault codes that may be the indoor coil sensor (ICT / RAT labels may be swapped)

# --- states of the "IFeel control status" sensor --------------------------------------------------------------------
OFF = "off"
ACTIVE = "active"
HOLDING_START = "holding_start"
COIL_GUARD = "coil_guard"
RUN_CAP = "run_cap"
SUSPENDED_AC_OFF = "suspended_ac_off"
SUSPENDED_FAN = "suspended_fan"
SUSPENDED_DRY = "suspended_dry"
SUSPENDED_SHABBAT = "suspended_shabbat"
STOPPED_SENSOR = "stopped_sensor"
STOPPED_SHABBAT = "stopped_shabbat"
STOPPED_REMOTE_MODE = "stopped_remote_mode"
STOPPED_REMOTE_IFEEL = "stopped_remote_ifeel"
STOPPED_VALUE_NOT_KEPT = "stopped_value_not_kept"
STOPPED_SENSOR_REMOVED = "stopped_sensor_removed"
REFUSED_NO_SLAVE1 = "refused_no_slave1"
REFUSED_COIL_FAULT = "refused_coil_fault"
REFUSED_AUTO = "refused_auto"
STATUSES = [
    OFF, ACTIVE, HOLDING_START, COIL_GUARD, RUN_CAP, SUSPENDED_AC_OFF, SUSPENDED_FAN, SUSPENDED_DRY, SUSPENDED_SHABBAT,
    STOPPED_SENSOR, STOPPED_SHABBAT, STOPPED_REMOTE_MODE, STOPPED_REMOTE_IFEEL, STOPPED_VALUE_NOT_KEPT,
    STOPPED_SENSOR_REMOVED, REFUSED_NO_SLAVE1, REFUSED_COIL_FAULT, REFUSED_AUTO,
]  # fmt: skip

# Internal phases while IFeel control is switched on
_ACTIVE, _SUSP_OFF, _SUSP_FAN, _SUSP_DRY = "active", "suspended_ac_off", "suspended_fan", "suspended_dry"
_SUSPENDED = (_SUSP_OFF, _SUSP_FAN, _SUSP_DRY)
# No IFeel in these modes (Dry: owner decision 2026-10-03): IFeel control is suspended in them
_SUSPEND_PHASE = {MODE_OFF: _SUSP_OFF, MODE_FAN: _SUSP_FAN, MODE_DRY: _SUSP_DRY}
_COOLING, _HEATING = "cooling", "heating"


@dataclass(frozen=True)
class IFeelConfig:
    """The user's settings (options)."""

    cold_tolerance: float = 0.5
    hot_tolerance: float = 0.5
    keepalive: float = 180.0  # rule 3
    min_off_minutes: int | None = None  # rule 6, Cool: None = the unit's 8-minute timer (recommended), else 3-8 minutes
    heat_min_off_minutes: int = 5  # rule 6, Heat (bit 10 never sets in Heat): timed from the stop seen, 3-8 minutes
    coil_limit: int = 5  # rule 8, Cool
    heat_coil_limit: int = 65  # rule 8, Heat: stop at or above this coil reading (60-75)
    stale_minutes: int = 30  # rule 7
    allow_without_coil_protection: bool = False  # safety gating override (not recommended)
    override_max_run_minutes: int = 10  # run cap while the override is in effect
    max_run_minutes: int = 45  # absolute run cap
    remote_off_on_keeps: bool = False  # the remote turning the AC off / on again in the same mode pauses, not stops
    shabbat_pauses: bool = False  # Shabbat mode (from the remote or Home Assistant) pauses, not stops


@dataclass(frozen=True)
class SensorReading:
    """The chosen room sensor. reported_at is on the controller's clock."""

    entity_id: str | None
    value: float | None  # °C; None when unavailable / unknown / not a number
    reported_at: float | None


@dataclass(frozen=True)
class Observation:
    status: Status  # the unit's own values (not the Standby display overrides)
    cells: Internal | None  # slave-1 cells read in this poll and laid out as expected, else None
    slave1_usable: bool  # slave 1 has not failed several polls in a row
    fresh: bool = True  # False for a fast status read or the readback after a settings write (no new slave-1 cells)


@dataclass(frozen=True)
class Write:
    address: int
    values: tuple[int, ...]
    reason: str
    delay: float = 0.0  # wait this long before this write (after the previous one)


@dataclass(frozen=True)
class Event:
    kind: str  # value_lost, recovery, check_now, stopped, expiry, remote_press, coil_guard, run_cap, warning, state
    data: dict = field(default_factory=dict)


class IFeelRefused(Exception):
    """IFeel control cannot be switched on; the message says why."""

    def __init__(self, reason: str, status: str | None = None) -> None:
        super().__init__(reason)
        self.status = status


Action = Write | Event


class IFeelController:
    def __init__(self, config: IFeelConfig, clock: Callable[[], float]) -> None:
        self.config = config
        self._clock = clock
        self.enabled = False
        self.stop_reason: str | None = None
        self._phase = _ACTIVE
        self.sensor: SensorReading | None = None
        self._sensor_bad_since: float | None = None
        self._last_temp: float | None = None  # last good sensor value (°C)
        self._obs: Observation | None = None
        self._cells: Internal | None = None
        self._cells_at = -math.inf
        self._known: tuple | None = None  # (mode, fan, setpoint) last seen
        self._expected: list[tuple[tuple, float]] = []  # own settings writes: (values, valid until)
        self.command = "off"  # thermostat command (Cool / Heat)
        self.value_written: int | None = None  # the IFeel value the unit should be using
        self._value_at = -math.inf  # last write of a value (verification grace)
        self._new_at = -math.inf  # last new value (rule 2)
        self._keepalive_at = -math.inf  # last enable or keep-alive written
        self._force = False  # write the next new value at once (setpoint / mode change, hold ended)
        self._resync = False  # a write failed: run the enable sequence again
        self._mismatch = 0
        self._recovery_step = 0
        self._recovery_at = -math.inf
        self._fast_mismatch = 0  # fast reads in a row with 0x3307 not the value written
        self._confirm = False  # the next fresh cells decide at once (fast detection asked for them)
        self._was_running: bool | None = None
        self._run_start: float | None = None
        self._stop_seen: float | None = None  # last compressor stop seen (bit 15 falling)
        self._stop_sent: float | None = None  # last stop value written (for the fallback without slave 1)
        self._start_sent: float | None = None  # first start value written after a stop value (ditto)
        self._coil_low_since: float | None = None
        self.coil_latch = False
        self._coil_latch_high = False  # the high-coil guard (Heat) is set: released at or below HEAT_COIL_RELEASE
        self._first_obs_at: float | None = None  # first poll seen (Heat hold-back when no stop has been seen yet)
        self.run_cap_latch = False
        self.held = False
        self.direction = _COOLING
        self.override_in_effect = False
        self._shabbat_cleanup = 0
        self._shabbat_paused = False  # Shabbat mode with config.shabbat_pauses: IFeel is off, nothing is written
        self._off_mode: int | None = None  # the mode the AC was in before it was turned off (None: not known)
        self._last_status: str | None = None

    # -- view -----------------------------------------------------------------------------------------------------

    @property
    def status(self) -> str:
        if not self.enabled:
            return self.stop_reason or OFF
        if self._shabbat_paused:
            return SUSPENDED_SHABBAT
        if self._phase == _SUSP_OFF:
            return SUSPENDED_AC_OFF
        if self._phase == _SUSP_FAN:
            return SUSPENDED_FAN
        if self._phase == _SUSP_DRY:
            return SUSPENDED_DRY
        if self.coil_latch:
            return COIL_GUARD
        if self.run_cap_latch:
            return RUN_CAP
        if self.held:
            return HOLDING_START
        return ACTIVE

    @property
    def needs_fast_reads(self) -> bool:
        """IFeel is on in the unit with a Home Assistant value: the status block must be read every second."""
        return self.enabled and self._phase == _ACTIVE and not self._shabbat_paused and self.value_written is not None

    @property
    def controlling(self) -> bool:
        """IFeel values from Home Assistant are in use (the climate card shows the room sensor)."""
        return self.status in (ACTIVE, HOLDING_START, COIL_GUARD, RUN_CAP)

    def attributes(self) -> dict:
        return {
            "command": self.command if self.enabled else None,
            "value": self.value_written if self.controlling else None,
            "sensor": self.sensor.entity_id if self.sensor else None,
            "sensor_value": self._last_temp,
            "override_in_effect": self.override_in_effect,
            "direction": self.direction,
        }

    # -- inputs ---------------------------------------------------------------------------------------------------

    def set_config(self, config: IFeelConfig) -> None:
        self.config = config

    def set_sensor(self, reading: SensorReading | None) -> list[Action]:
        """A new reading of the chosen sensor, or a different sensor (None: none chosen)."""
        old = self.sensor
        self.sensor = reading
        if reading is not None and reading.value is not None:
            self._last_temp = reading.value
        if old is not None and reading is not None and old.entity_id != reading.entity_id:
            self._last_temp = reading.value
            self._sensor_bad_since = None
        if self._obs is None or not self.enabled:
            return []
        return self._step(self._obs, sensor_only=True)

    def expect_own_settings(self, mode: int | None, fan: int | None, setpoint: int | None) -> None:
        """The integration is about to write these settings: a change to them is not remote use."""
        now = self._clock()
        self._expected = [(v, t) for v, t in self._expected if t > now] + [((mode, fan, setpoint), now + OWN_SETTINGS_WINDOW)]

    def write_failed(self, write: Write) -> None:
        """A write was not acknowledged: redo the enable sequence at the next step."""
        if self.enabled and self._phase == _ACTIVE and not self._shabbat_paused:
            self._resync = True

    def update(self, obs: Observation) -> list[Action]:
        """One poll (fresh) or the readback after a settings write."""
        return self._step(obs)

    def request_enable(self) -> list[Action]:
        """The user switches IFeel control on (also used to restore it after a restart). Raises IFeelRefused."""
        obs = self._obs
        if obs is None:
            raise IFeelRefused("no data from the AC yet")
        if self.sensor is None or self.sensor.entity_id is None:
            raise IFeelRefused("no room sensor chosen", STOPPED_SENSOR_REMOVED)
        if obs.status.mode == MODE_AUTO:
            # Auto is not supported (owner decision 2026-10-04: on this unit it can freeze the indoor coil)
            self.stop_reason = REFUSED_AUTO
            raise IFeelRefused("the AC is in Auto mode, which IFeel control does not support", REFUSED_AUTO)
        paused = self._shabbat_on(obs)
        if paused and not self.config.shabbat_pauses:
            raise IFeelRefused("Shabbat mode is on", STOPPED_SHABBAT)
        reason = self._gate(obs)
        if reason is not None:
            self.stop_reason = reason
            raise IFeelRefused(
                "the indoor coil cannot be watched (slave 1 not usable)" if reason == REFUSED_NO_SLAVE1
                else "indoor coil temperature sensor fault", reason,
            )  # fmt: skip
        problem = self._sensor_problem(self._clock())
        if problem:
            raise IFeelRefused(problem, STOPPED_SENSOR)
        self.enabled = True
        self.stop_reason = None
        self._shabbat_cleanup = 0
        self._shabbat_paused = paused  # enabled during Shabbat: starts when Shabbat ends
        self._off_mode = None
        self._reset()
        if not obs.slave1_usable:
            # the compressor's state is unknown: act as if it had just stopped (rule 6 fallback, conservative)
            self._stop_sent = self._clock() - NO_SLAVE1_RUN_MARGIN
        self.command = "off"  # starting state between the switching points (decided)
        self._evaluate_command(obs.status)
        acts: list[Action] = [Event("state", {"ifeel_control": "on"})]
        mode = obs.status.mode
        if mode in _SUSPEND_PHASE:
            self._phase = _SUSPEND_PHASE[mode]
        else:
            self._phase = _ACTIVE
            if not paused:
                acts += self._enable_sequence(obs, "enable")
        return acts + self._state_event()

    def request_disable(self) -> list[Action]:
        """The user switches IFeel control off."""
        acts = self._handback("disabled by the user") if self.enabled else []
        self.enabled = False
        self.stop_reason = None
        self._shabbat_paused = False
        return acts + [Event("state", {"ifeel_control": "off"})] + self._state_event()

    def stop(self, status: str, detail: str | None = None) -> list[Action]:
        """Stop with a reason and hand back (used when IFeel control cannot be restored after a reload, which leaves
        IFeel on in the unit)."""
        return self._stop(status, detail)

    def handback_for_unload(self) -> list[Action]:
        """The integration is unloaded or Home Assistant stops: hand back, but stay enabled (restored later)."""
        if not self.enabled:
            return []
        return self._handback("unload / stop")

    # -- the step -------------------------------------------------------------------------------------------------

    def _step(self, obs: Observation, sensor_only: bool = False) -> list[Action]:
        now = self._clock()
        acts: list[Action] = []
        st = obs.status
        if not sensor_only:
            self._obs = obs
            if self._first_obs_at is None:
                self._first_obs_at = now
            if obs.fresh and obs.cells is not None:
                self._cells, self._cells_at = obs.cells, now
            changed_by, old = self._settings_change(st)
            self._track_compressor(obs, now)
        else:
            changed_by, old = None, None
        cells = obs.cells if (obs.fresh and not sensor_only) else None

        # After a stop for Shabbat: when Shabbat goes off the unit may switch IFeel back on by itself (seen 2026-10-01),
        # so write IFeel off again until bit 14 is seen clear (a few tries; without slave 1, a few writes)
        if not sensor_only and self._shabbat_cleanup and not self._shabbat_on(obs):
            if cells is not None and not cells.ifeel_active:
                self._shabbat_cleanup = 0
            else:
                self._shabbat_cleanup -= 1
                acts.append(Write(REG_IFEEL, (0,), "Shabbat ended: keep IFeel off"))
                if self._shabbat_cleanup == 0 and obs.slave1_usable:
                    acts.append(Event("warning", {"message": "IFeel stays on after Shabbat ended"}))
        if not self.enabled:
            return acts + self._state_event()

        shabbat_ended = False
        if self._shabbat_on(obs):
            if not self.config.shabbat_pauses:
                self._shabbat_cleanup = SHABBAT_CLEANUP_TRIES
                return acts + self._stop(STOPPED_SHABBAT)
            if not self._shabbat_paused:
                # pause: hand back as for a stop (the unit turns Modbus IFeel off by itself), then write nothing
                acts += self._handback("Shabbat mode")
                self._shabbat_paused = True
        elif self._shabbat_paused and not sensor_only:
            self._shabbat_paused = False
            shabbat_ended = True
        if not self._shabbat_paused:
            reason = self._gate(obs)
            if reason is not None:
                return acts + self._stop(reason)
            problem = self._sensor_problem(now)
            if problem:
                status = STOPPED_SENSOR_REMOVED if problem == "removed" else STOPPED_SENSOR
                return acts + self._stop(status, problem)

        # settings changes (rule 5 / rule 9 / rule 11)
        if changed_by == "remote":
            if old[0] != st.mode:
                if not (self.config.remote_off_on_keeps and MODE_OFF in (old[0], st.mode)):
                    return acts + self._stop(STOPPED_REMOTE_MODE)
                # turned off, or on again, at the remote (setting): as from Home Assistant, but on again only in the
                # mode it was turned off in
                if st.mode == MODE_AUTO or (st.mode != MODE_OFF and self._off_mode is not None and st.mode != self._off_mode):
                    return acts + self._stop(STOPPED_REMOTE_MODE, "turned on at the remote in another mode")
                acts += self._off_on_change(obs, old)
                if self._phase != _ACTIVE or self._shabbat_paused:
                    return acts + self._state_event()
            # fan / setpoint at the remote: Home Assistant wins with the new settings (the press turned IFeel off)
            elif self._phase == _ACTIVE and not self._shabbat_paused:
                self._evaluate_command(st)
                return acts + self._enable_sequence(obs, "remote fan / setpoint change: take IFeel back") + self._state_event()
        elif changed_by == "own":
            acts += self._own_settings_change(obs, old)
            if self._phase != _ACTIVE or self._shabbat_paused:
                return acts + self._state_event()

        if self._phase in _SUSPENDED:
            if shabbat_ended:
                self._shabbat_cleanup = SHABBAT_CLEANUP_TRIES  # the unit may switch IFeel back on by itself
            return acts + self._state_event()
        if self._shabbat_paused:
            return acts + self._state_event()
        if shabbat_ended:
            self.command = "off"
            self._evaluate_command(st)
            return acts + self._enable_sequence(obs, "Shabbat mode ended: resume") + self._state_event()

        # active
        if not obs.fresh and not sensor_only:
            acts += self._fast_check(st, now)
        if self._resync:
            self._resync = False
            return acts + self._enable_sequence(obs, "resync after a failed write") + self._state_event()
        if cells is not None and not sensor_only:
            verdict = self._verify(obs, cells, now)
            if verdict is not None:
                return acts + verdict + self._state_event()
        elif not obs.slave1_usable and not sensor_only and obs.fresh:
            verdict = self._verify_without_slave1(st, now)
            if verdict is not None:
                return acts + verdict + self._state_event()

        self._evaluate_command(st)
        acts += self._safety(now)
        acts += self._write_desired(st, now)
        # rule 3: no keep-alive while a lost value is being checked (a keep-alive does not bring a
        # dropped value back; the recovery's block write does, and refreshes IFeel too)
        loss_suspected = self._fast_mismatch or self._mismatch or self._confirm
        if now - self._keepalive_at >= self.config.keepalive and not loss_suspected:
            acts += self._keepalive(obs)
        return acts + self._state_event()

    # -- settings changes -----------------------------------------------------------------------------------------

    def _settings_change(self, st: Status) -> tuple[str | None, tuple | None]:
        settings = (st.mode, st.fan, st.setpoint)
        old = self._known
        self._known = settings
        if old is None or settings == old:
            return None, old
        now = self._clock()
        self._expected = [(v, t) for v, t in self._expected if t > now]
        for values, _ in self._expected:
            if values == settings:
                self._expected = [(v, t) for v, t in self._expected if v != values]
                return "own", old
        return "remote", old

    def _own_settings_change(self, obs: Observation, old: tuple) -> list[Action]:
        st = obs.status
        if st.mode in _SUSPEND_PHASE or self._phase in _SUSPENDED:
            return self._off_on_change(obs, old)
        acts: list[Action] = []
        if self._phase == _ACTIVE:
            if _family(old[0]) != _family(st.mode):
                self.command = "off"
            self._force = True  # rule 11 / rule 9: the value means something else now
        return acts

    def _off_on_change(self, obs: Observation, old: tuple) -> list[Action]:
        """The AC turned off / set to Fan or Dry (pause), or on again / to Cool or Heat (resume): from Home
        Assistant, or from the remote with config.remote_off_on_keeps. Nothing is written while Shabbat mode pauses IFeel
        control."""
        st = obs.status
        acts: list[Action] = []
        if st.mode in _SUSPEND_PHASE:
            if self._phase == _ACTIVE:
                acts += self._handback({MODE_OFF: "AC off", MODE_FAN: "Fan mode", MODE_DRY: "Dry mode"}[st.mode])
            if st.mode == MODE_OFF and old[0] != MODE_OFF:
                self._off_mode = old[0]
            self._phase = _SUSPEND_PHASE[st.mode]
            return acts
        if self._phase in _SUSPENDED:
            self._phase = _ACTIVE
            self.command = "off"
            self._evaluate_command(st)
            if not self._shabbat_paused:
                acts += self._enable_sequence(obs, "AC on again: resume")
        return acts

    # -- verification and recovery ---------------------------------------------------------------------------------

    def _fast_check(self, st: Status, now: float) -> list[Action]:
        """Fast detection: 0x3307 shows the value written while it is kept and the remote's cached value once it is dropped.
        Two fast reads in a row with another value ask for the slave-1 cells at once."""
        if self.value_written is None or now - self._value_at < VERIFY_GRACE or self._confirm:
            return []
        if st.ifeel_temp == self.value_written:
            self._fast_mismatch = 0
            return []
        self._fast_mismatch += 1
        if self._fast_mismatch < FAST_MISMATCHES:
            return []
        self._fast_mismatch = 0
        self._confirm = True
        return [Event("check_now", {"written": self.value_written, "reg_3307": st.ifeel_temp})]

    def _verify(self, obs: Observation, cells: Internal, now: float) -> list[Action] | None:
        if now - self._value_at < VERIFY_GRACE:
            return None
        if not cells.ifeel_active:
            self._mismatch = 0
            if now - self._keepalive_at > EXPIRY_AFTER:
                return [Event("expiry", {})] + self._enable_sequence(obs, "IFeel expired: re-enable")
            # a remote press without a mode change (or one whose mode change has not shown yet): take IFeel back
            return [Event("remote_press", {"marker": cells.marker})] + self._enable_sequence(
                obs, "remote press: take IFeel back"
            )
        if cells.ifeel_source == IFEEL_SOURCE_REMOTE:
            return self._stop(STOPPED_REMOTE_IFEEL)
        if cells.ifeel_source != IFEEL_SOURCE_MODBUS:
            return None
        return self._check_value(obs, cells.mirror, now, cells.marker)

    def _verify_without_slave1(self, st: Status, now: float) -> list[Action] | None:
        """Without slave 1 only 0x3307 can be compared (it shows the remote's cached value once the value is dropped)."""
        if now - self._value_at < VERIFY_GRACE or self._obs is None:
            return None
        return self._check_value(self._obs, st.ifeel_temp, now, None)

    def _check_value(self, obs: Observation, in_use: int, now: float, marker: bool | None) -> list[Action] | None:
        """Rule 4: a lost value is re-sent at once, once more if lost again within 30 s; a third loss stops IFeel
        control. Outside a recovery window a loss must show on two polls in a row, unless fast detection asked for
        this check (then this one decides)."""
        confirm, self._confirm = self._confirm, False
        in_window = self._recovery_step > 0 and now - self._recovery_at < RECOVERY_HOLD
        if in_use == self.value_written:
            self._mismatch = 0
            if self._recovery_step and not in_window:
                self._recovery_step = 0
            return None
        if marker:
            self._mismatch = 0  # explained by a remote command
            return None
        if self._recovery_step and not in_window:
            self._recovery_step = 0
        self._mismatch += 1
        if not in_window and not confirm and self._mismatch < 2:
            return None
        self._mismatch = 0
        event = Event(
            "value_lost",
            {
                "written": self.value_written,
                "in_use": in_use,
                "written_at": self._value_at,
                "since_write": round(now - self._value_at, 1),
                "attempt": self._recovery_step,
            },
        )
        # a dropped value means "heat" in Heat (rule 4): there one failed recovery is enough
        if self._recovery_step >= (1 if self._heating(obs.status) else 2):
            return [event] + self._stop(STOPPED_VALUE_NOT_KEPT, "the AC did not keep the IFeel values")
        self._recovery_step += 1
        self._recovery_at = now
        return [event, Event("recovery", {"attempt": self._recovery_step})] + self._enable_sequence(
            obs, f"recovery {self._recovery_step}"
        )

    # -- safety ---------------------------------------------------------------------------------------------------

    def _gate(self, obs: Observation) -> str | None:
        """Safety gating: None when IFeel control may run; sets override_in_effect."""
        st = obs.status
        reason = None
        if not obs.slave1_usable:
            reason = REFUSED_NO_SLAVE1
        else:
            cells = self._recent_cells()
            coil_missing = cells is not None and cells.coil_temp is None
            if st.idu_fault in COIL_FAULT_CODES or coil_missing:
                reason = REFUSED_COIL_FAULT
        if reason is not None and self.config.allow_without_coil_protection:
            self.override_in_effect = True
            return None
        self.override_in_effect = False
        return reason

    def _safety(self, now: float) -> list[Action]:
        """Coil guard (rule 8) and run-time caps: set the latches; the value follows in _write_desired."""
        acts: list[Action] = []
        cells = self._recent_cells()
        running = self._running(now)
        st = self._obs.status if self._obs else None
        # low coil (freezing): Cool, and Heat too since 2026-10-04 (a frozen coil was seen in Auto with the Heat bit set)
        low_guard = st is not None and st.mode in (MODE_COOL, MODE_HEAT)
        heating = st is not None and self._heating(st)
        if cells is not None and cells.coil_temp is not None:
            coil = cells.coil_temp
            if self.coil_latch and (coil <= HEAT_COIL_RELEASE if self._coil_latch_high else coil > COIL_RELEASE):
                self.coil_latch = False
                acts.append(Event("coil_guard", {"state": "released", "coil": coil}))
            if running and heating and coil >= self.config.heat_coil_limit and not self.coil_latch:
                # Heat (decided 2026-10-03): at once; the unit regulates its coil at about 61 C by itself
                self.coil_latch = self._coil_latch_high = True
                self._force = True
                acts.append(Event("coil_guard", {"state": "active", "coil": coil, "limit": "high"}))
            if running and low_guard and coil <= self.config.coil_limit:
                self._coil_low_since = self._coil_low_since or now
                if not self.coil_latch and now - self._coil_low_since >= COIL_FOR:
                    self.coil_latch = True
                    self._coil_latch_high = False
                    self._force = True
                    acts.append(Event("coil_guard", {"state": "active", "coil": coil, "limit": "low"}))
            else:
                self._coil_low_since = None
        if running and self._run_start is not None and not self.run_cap_latch:
            limit = self.config.max_run_minutes * 60
            if self.override_in_effect:
                limit = min(limit, self.config.override_max_run_minutes * 60)
            if now - self._run_start >= limit:
                self.run_cap_latch = True
                self._force = True
                acts.append(Event("run_cap", {"minutes": round((now - self._run_start) / 60, 1)}))
        return acts

    # -- compressor tracking --------------------------------------------------------------------------------------

    def _track_compressor(self, obs: Observation, now: float) -> None:
        cells = obs.cells if obs.fresh else None
        if cells is None:
            if not obs.slave1_usable and self.run_cap_latch and self._stop_sent is not None:
                if now >= self._stop_sent + NO_SLAVE1_RUN_MARGIN:
                    self.run_cap_latch = False
            return
        running = cells.running
        if self._was_running is not None and running != self._was_running:
            if running:
                self._run_start = now
            else:
                self._stop_seen = now
                self._run_start = None
                self.run_cap_latch = False
        elif self._was_running is None and running:
            self._run_start = now  # already running when first seen: count from now
        self._was_running = running

    def _running(self, now: float) -> bool:
        cells = self._recent_cells()
        if cells is not None:
            return cells.running
        if self._obs is not None and not self._obs.slave1_usable:
            # unknown: assume running from the first start value sent until a stop value plus the minimum run
            return self._start_sent is not None
        return False

    def _recent_cells(self) -> Internal | None:
        if self._cells is None or self._clock() - self._cells_at > CELLS_MAX_AGE:
            return None
        if self._obs is not None and not self._obs.slave1_usable:
            return None
        return self._cells

    def _timer_active(self, now: float) -> bool:
        """Rule 6: is a start held back now? Cool: bit 10 (or the custom minutes); Heat: its own minutes, timed from the
        stop seen (bit 10 never sets in Heat; decided 2026-10-03)."""
        mode = self._obs.status.mode if self._obs is not None else None
        if mode == MODE_HEAT:
            return self._heat_timer_active(now)
        return self._cool_timer_active(now)

    def _cool_timer_active(self, now: float) -> bool:
        cells = self._recent_cells()
        minutes = self.config.min_off_minutes
        if cells is not None:
            if minutes is None or self._stop_seen is None:
                return cells.lockout
            return now - self._stop_seen < minutes * 60
        return self._no_slave1_timer_active(now, TIMER_SECONDS if minutes is None else minutes * 60)

    def _heat_timer_active(self, now: float) -> bool:
        off_time = self.config.heat_min_off_minutes * 60
        if self._recent_cells() is not None:
            since = self._stop_seen if self._stop_seen is not None else self._first_obs_at
            return since is not None and now - since < off_time
        return self._no_slave1_timer_active(now, off_time)

    def _no_slave1_timer_active(self, now: float, off_time: float) -> bool:
        """Without slave 1: after a stop value, assume up to NO_SLAVE1_RUN_MARGIN more running, then the off time."""
        if self._stop_sent is None:
            return False
        return now < self._stop_sent + NO_SLAVE1_RUN_MARGIN + off_time

    # -- thermostat and values ------------------------------------------------------------------------------------

    def _evaluate_command(self, st: Status) -> None:
        temp, sp = self._last_temp, st.setpoint
        if temp is None or sp is None:
            return
        if st.mode == MODE_COOL:
            self.direction = _COOLING
            if temp >= sp + self.config.hot_tolerance:
                self.command = "on"
            elif temp <= sp - self.config.cold_tolerance:
                self.command = "off"
        elif st.mode == MODE_HEAT:
            self.direction = _HEATING
            if temp <= sp - self.config.cold_tolerance:
                self.command = "on"
            elif temp >= sp + self.config.hot_tolerance:
                self.command = "off"

    def _command_value(self, st: Status) -> int | None:
        sp = st.setpoint
        if sp is None or self._last_temp is None:
            return None
        on = self.command == "on"
        if st.mode == MODE_HEAT:
            return sp - 1 if on else sp + 1
        return sp + 1 if on else sp - 1

    def _stop_value(self, st: Status) -> int:
        sp = st.setpoint
        if st.mode == MODE_HEAT:
            return sp + 1
        return sp - 1

    def _held_value(self, st: Status) -> int:
        """Rule 6: Cool keeps setpoint - 1, Heat setpoint + 1 (the stop values)."""
        return self._stop_value(st)

    def _would_start(self, value: int | None, st: Status) -> bool:
        sp = st.setpoint
        if value is None or sp is None:
            return False
        if st.mode == MODE_COOL:
            return value >= sp + 1
        if st.mode == MODE_HEAT:
            return value <= sp - 1
        return False

    def _desired(self, st: Status, now: float) -> int | None:
        if st.setpoint is None:
            return None
        if self.coil_latch or self.run_cap_latch:
            self.held = False
            return self._stop_value(st)
        base = self._command_value(st)
        if base is None:
            return None
        idle = not self._running(now)
        held = (
            self._would_start(base, st)
            and idle
            and self._timer_active(now)
            and not self._would_start(self.value_written, st)  # a start already sent is not taken back
        )
        if self.held and not held:
            self._force = True  # the hold ended: "on" at once
        self.held = held
        return self._held_value(st) if held else base

    def _write_desired(self, st: Status, now: float) -> list[Action]:
        value = self._desired(st, now)
        if value is None or value == self.value_written:
            self._force = False
            return []
        if not self._force and now - self._new_at < NEW_VALUE_INTERVAL:
            return []  # rule 2: held until 180 s after the last new value (the latest value wins)
        self._force = False
        return self._write_value(st, value, "new value")

    def _write_value(self, st: Status, value: int, reason: str) -> list[Action]:
        now = self._clock()
        self._note_value(st, value, now)
        self._value_at = now
        self._new_at = now
        return [Write(REG_IFEEL_TEMP, (value,), reason)]

    def _note_value(self, st: Status, value: int, now: float) -> None:
        """Track start and stop values sent (the compressor's state without slave 1, and the off-time timer)."""
        starts, was_start = self._would_start(value, st), self._would_start(self.value_written, st)
        no_slave1 = self._obs is not None and not self._obs.slave1_usable
        if starts and not was_start:
            self._start_sent = now
            if no_slave1:
                self._run_start = now
        elif not starts and was_start:
            self._stop_sent = now
            if no_slave1:
                self._start_sent = None
                self._run_start = None
        self.value_written = value

    # -- writes ---------------------------------------------------------------------------------------------------

    def _enable_sequence(self, obs: Observation, reason: str, value: int | None = None) -> list[Action]:
        """Rule 1: [1, value] as one block, the value again 1.5 s later."""
        st = obs.status
        now = self._clock()
        if value is None:
            value = self._desired(st, now)
        if value is None:
            value = self._stop_value(st) if st.setpoint is not None else SAFE_COOLING
        value = max(10, min(40, value))
        self._note_value(st, value, now)
        self._value_at = now + ENABLE_REPEAT_DELAY
        self._new_at = now
        self._keepalive_at = now
        self._mismatch = 0
        self._force = False
        self._shabbat_cleanup = 0  # IFeel is wanted on now
        return [
            Write(REG_IFEEL, (1, value), f"enable ({reason})"),
            Write(REG_IFEEL_TEMP, (value,), f"enable repeat ({reason})", delay=ENABLE_REPEAT_DELAY),
        ]

    def _keepalive(self, obs: Observation) -> list[Action]:
        self._keepalive_at = self._clock()
        if not obs.slave1_usable and self.value_written is not None:
            # without slave 1 nothing can be verified: re-assert the value with every keep-alive
            self._value_at = self._keepalive_at
            return [Write(REG_IFEEL, (1, self.value_written), "keep-alive with value (no slave 1)")]
        return [Write(REG_IFEEL, (1,), "keep-alive")]

    def _handback(self, why: str) -> list[Action]:
        """Rule 1 / rule 10: the safe value, then IFeel off (only if IFeel may be on)."""
        if self._phase in _SUSPENDED or self._shabbat_paused:
            return []  # IFeel is already off
        safe = SAFE_COOLING if self.direction == _COOLING else SAFE_HEATING
        self.value_written = None
        self.held = False
        return [
            Write(REG_IFEEL_TEMP, (safe,), f"safe value before IFeel off ({why})"),
            Write(REG_IFEEL, (0,), f"IFeel off ({why})"),
        ]

    def _stop(self, status: str, detail: str | None = None) -> list[Action]:
        acts = self._handback(status)
        self.enabled = False
        self.stop_reason = status
        self._phase = _ACTIVE
        self._shabbat_paused = False
        return acts + [Event("stopped", {"status": status, "detail": detail})] + self._state_event()

    def _reset(self) -> None:
        self.value_written = None
        self._value_at = self._new_at = self._keepalive_at = -math.inf
        self._force = self._resync = False
        self._mismatch = self._recovery_step = self._fast_mismatch = 0
        self._confirm = False
        self.coil_latch = self.run_cap_latch = self.held = False
        self._coil_latch_high = False
        self._coil_low_since = None
        self._sensor_bad_since = None

    # -- helpers --------------------------------------------------------------------------------------------------

    def _heating(self, st: Status) -> bool:
        return st.mode == MODE_HEAT

    def _shabbat_on(self, obs: Observation) -> bool:
        cells = obs.cells if obs.fresh else None
        return obs.status.shabat or (cells is not None and cells.shabat)

    def _sensor_problem(self, now: float) -> str | None:
        s = self.sensor
        if s is None or s.entity_id is None:
            return "removed"
        if s.value is None:
            self._sensor_bad_since = self._sensor_bad_since or now
            if now - self._sensor_bad_since > SENSOR_GRACE:
                return "room sensor unavailable"
            return None
        self._sensor_bad_since = None
        if not SENSOR_MIN <= s.value <= SENSOR_MAX:
            return f"room sensor reads {s.value} °C, outside {SENSOR_MIN:.0f}-{SENSOR_MAX:.0f} °C"
        if s.reported_at is not None and now - s.reported_at > self.config.stale_minutes * 60:
            return f"room sensor has not reported for {(now - s.reported_at) / 60:.0f} minutes"
        return None

    def _state_event(self) -> list[Action]:
        status = self.status
        if status == self._last_status:
            return []
        self._last_status = status
        return [Event("state", {"status": status, "value": self.value_written, "command": self.command})]


def _family(mode: int | None) -> str | None:
    if mode == MODE_COOL:
        return _COOLING
    if mode == MODE_HEAT:
        return _HEATING
    return None if mode is None else str(mode)

