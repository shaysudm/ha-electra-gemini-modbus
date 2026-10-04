# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs the IFeel controller against the unit model on a virtual clock, the way the coordinator does (no network)."""
from __future__ import annotations

from dataclasses import replace

from custom_components.electra_gemini.ifeel import (
    Event,
    IFeelConfig,
    IFeelController,
    Observation,
    SensorReading,
    Write,
)
from custom_components.electra_gemini.modbus import check_write
from custom_components.electra_gemini.registers import (
    coil_plausible,
    marker_seen,
    parse_internal,
    parse_status,
)
from unit_model import UnitModel

SENSOR = "sensor.bedroom"


class Harness:
    def __init__(
        self,
        config: IFeelConfig | None = None,
        *,
        poll_every: float = 10.0,
        sensor_every: float = 60.0,
        sensor_offset: float = 0.0,
        **model,
    ) -> None:
        self.t = 0.0
        self.unit = UnitModel(self.clock, **model)
        self.ctrl = IFeelController(config or IFeelConfig(), self.clock)
        self.poll_every, self.sensor_every, self.sensor_offset = poll_every, sensor_every, sensor_offset
        self.sensor_value: float | None = None  # fixed value; None = follow the model's room
        self.sensor_available = True
        self.sensor_reports = True
        self.sensor_entity: str | None = SENSOR
        self.slave1_usable = True
        self.fast_reads = True  # the coordinator reads the status block every second while IFeel is on
        self.fast_read_count = 0
        self.events: list[Event] = []
        self.writes: list[tuple[float, Write]] = []
        self.violations: list[str] = []
        self._last_poll = -1e9
        self._last_sensor = -1e9
        self._reported_at = 0.0
        self.poll()
        self.report_sensor()

    def clock(self) -> float:
        return self.t

    # -- feeding the controller -----------------------------------------------------------------------------------

    def observe(self, fresh: bool = True) -> Observation:
        self.unit.note_block_read()  # every status read is a block read of slave 160
        status = parse_status(self.unit.regs160())
        cells = None
        if self.slave1_usable and self.unit.slave1 and fresh:
            try:
                cells = parse_internal(self.unit.regs4801(), status.room_temp, status.mode)
            except ValueError:
                cells = None
            if cells is not None:
                if cells.coil_temp is not None and not coil_plausible(cells.coil_temp, status, cells.running):
                    cells = replace(cells, coil_temp=None)
                cells = replace(cells, marker=marker_seen(self.unit.marker()))
        return Observation(status, cells, self.slave1_usable and self.unit.slave1, fresh)

    def poll(self) -> None:
        self._last_poll = self.t
        self.apply(self.ctrl.update(self.observe()))

    def reading(self) -> SensorReading:
        if self.sensor_entity is None:
            return None
        value = self.sensor_value if self.sensor_value is not None else round(self.unit.room + self.sensor_offset, 2)
        if not self.sensor_available:
            value = None
        return SensorReading(self.sensor_entity, value, self._reported_at)

    def report_sensor(self) -> None:
        self._last_sensor = self.t
        if self.sensor_reports:
            self._reported_at = self.t
        self.apply(self.ctrl.set_sensor(self.reading()))

    def apply(self, actions) -> None:
        check_now = False
        for action in actions:
            if isinstance(action, Event):
                self.events.append(action)
                check_now = check_now or action.kind == "check_now"
                continue
            if action.delay:
                self.advance(action.delay)
            self.check(action)
            self.writes.append((self.t, action))
            self.unit.write(action.address, action.values)
        if check_now:
            self.poll()  # fast detection asked for the slave-1 cells at once

    def check(self, w: Write) -> None:
        try:
            check_write(160, 160, w.address, list(w.values))
        except ValueError as err:
            self.violations.append(f"t={self.t:.0f} not allowed: {err}")
        value = w.values[-1] if w.address == 0x3307 or len(w.values) == 2 else None
        u = self.unit
        if value is None or u.mode not in (1, 2, 3, 4):
            return
        sp = u.setpoint
        starts = {1: value >= sp + 1, 4: value >= sp + 1, 2: value <= sp - 1, 3: value >= sp + 1 or value <= sp - 1}[u.mode]
        if not starts or u.running:
            return
        if u.mode in (1, 3) and u.timer_bit() and self.ctrl.config.min_off_minutes is None:
            self.violations.append(f"t={self.t:.0f} start value {value} sent inside the 8-minute window ({w.reason})")
        heat_off = self.ctrl.config.heat_min_off_minutes * 60
        if u.mode in (2, 3) and self.t - u.last_stop < heat_off:
            # Heat has no 8-minute bit: its own minimum off time, counted from the unit's last stop
            self.violations.append(f"t={self.t:.0f} start value {value} sent {self.t - u.last_stop:.0f} s after a stop in "
                                   f"Heat ({w.reason})")

    # -- running --------------------------------------------------------------------------------------------------

    def advance(self, seconds: float) -> None:
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.t = min(end, self.t + 0.5)
            self.unit.advance()

    def fast_read(self) -> None:
        self.fast_read_count += 1
        self.apply(self.ctrl.update(self.observe(fresh=False)))

    def run(self, seconds: float) -> None:
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.advance(min(1.0, end - self.t))
            if self.t - self._last_sensor >= self.sensor_every:
                self.report_sensor()
            if self.t - self._last_poll >= self.poll_every:
                self.poll()
            elif self.fast_reads and self.ctrl.needs_fast_reads:
                self.fast_read()

    def enable(self) -> None:
        self.apply(self.ctrl.request_enable())

    def own_settings(self, mode=None, fan=None, setpoint=None) -> None:
        """A settings write by the integration (like the coordinator: announce, write, read back)."""
        u = self.unit
        new = (u.mode if mode is None else mode, u.fan if fan is None else fan, u.setpoint if setpoint is None else setpoint)
        self.ctrl.expect_own_settings(*new)
        if mode is not None or fan is not None:
            u.write(0x3300, new)
        else:
            u.write(0x3302, (new[2],))
        self.advance(0.6)
        self.apply(self.ctrl.update(self.observe(fresh=False)))

    # -- queries --------------------------------------------------------------------------------------------------

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def values_written(self) -> list[int]:
        return [w.values[-1] for _, w in self.writes if w.address == 0x3307 or len(w.values) == 2]

    def last_value_write(self) -> Write:
        return [w for _, w in self.writes if w.address == 0x3307 or len(w.values) == 2][-1]
