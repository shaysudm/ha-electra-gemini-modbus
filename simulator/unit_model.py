# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A model of the Electra/Airwell GEMINI IDU on any clock (no network), for the IFeel tests.

What it reproduces (docs/REGISTER_MAP.md, "IFeel driven over Modbus", and docs/IFEEL_DESIGN.md):
* IFeel input: 0x3306 = 1 enables Modbus IFeel (bit 14 of 0x4801, 0x4803 = 0x2000) and restores the last Modbus value
  (or 0); [1, value] as one block takes the value at once; a value write to 0x3307 while 0x3306 reads 1 turns IFeel back
  on; IFeel expires 423 s after the last enable / keep-alive, while 0x3306 keeps reading 1; 0x3306 = 0 switches it off.
  0x3307 shows the last Modbus value while it is in use, the remote's cached value otherwise. The value in use is the
  "mirror" (0x4809 high byte).
* The read rate (found 2026-10-02): a Modbus IFeel value is kept only while slave 160 is read in blocks at least every
  read_gap_limit seconds (2.5), counted from the last block read or the last value write; otherwise it is dropped
  (mirror 0, 0x3307 shows the cached value), while bit 14 and 0x4803 still say Modbus IFeel. note_block_read() is
  called for every status-block read.
* Unexplained drops (erasing = True, for tests): every value written is dropped erase_delay seconds later, reads or not.
* Remote: a button press switches IFeel off inside the unit (0x3306 keeps reading 1), applies its setting change and
  shows the IR frame marker for 3 s; the remote's own IFeel takes IFeel over (0x4803 = 0x1000).
* Shabbat on switches IFeel off inside the unit and asks for a compressor start; Shabbat off brings IFeel back.
* Compressor: on the value in use (IFeel) or the board's own room reading; Cool / Dry start at setpoint + 1 and stop at
  setpoint - 1, Heat the reverse, the setpoint holds the current state; at least 180 s off and 180 s on; start pending
  1.2 s, running 3 s after a start demand; 0x4805 bit 10 set while running and for 480 s after a stop, **except in Heat,
  where it is never set** (2026-10-03). A dropped value (0) means "very cold": in Heat the unit heats on it.
* Room and coil: the room cools while cooling runs (heats while heating runs) and drifts otherwise; the coil falls while
  cooling runs and recovers when idle; in Heat it rises to heat_coil_peak (60 °C; set higher to model a fault) at
  heat_coil_rate while running and falls back about 28 °C a minute after a stop.
* 0x4801 mode bits: Cool 0x0800, Heat 0x0100, Auto 0x1000, Dry 0x0400, Fan 0x0200; 0x4805 bit 11 in Heat (and in Auto
  once it chose heating); the coil byte is signed.
"""
from __future__ import annotations

from collections.abc import Callable

MODE_BITS = {1: 0x0800, 2: 0x0100, 3: 0x1000, 4: 0x0400, 5: 0x0200}
EXPIRY = 423.0
MIN_RUN = 180.0
MIN_OFF = 180.0
TIMER = 480.0


class UnitModel:
    def __init__(
        self,
        clock: Callable[[], float],
        *,
        mode: int = 1,
        fan: int = 1,
        setpoint: int = 24,
        room: float = 24.3,
        cached_remote: int = 18,
        room_rate: float = 0.6,  # °C per minute while the compressor runs
        drift: float = 0.25,  # °C per minute towards warm (Cool) / cold (Heat) while idle
        coil_rate: float = 2.0,  # °C per minute while cooling runs
        coil_floor: float = 9.0,
        heat_coil_rate: float = 10.0,  # °C per minute while heating runs (about 30 to 60 °C in 3 minutes)
        heat_coil_peak: float = 60.0,
        idu_fault: int = 0,
        last_stop_ago: float = 600.0,
    ) -> None:
        self.clock = clock
        self.mode, self.fan, self.setpoint = mode, fan, setpoint
        self.shabat = False
        self.sleep = False
        self.idu_fault = idu_fault
        self.room = room
        self.coil = room
        self.room_rate, self.drift, self.coil_rate, self.coil_floor = room_rate, drift, coil_rate, coil_floor
        self.heat_coil_rate, self.heat_coil_peak = heat_coil_rate, heat_coil_peak
        self.freeze = False  # a fault (seen in Auto, 2026-10-04): the coil falls to -25 °C while running, in any mode
        # IFeel
        self.flag = 0
        self.active = False
        self.source = 0
        self.cached = cached_remote
        self.mirror = cached_remote
        self.last_modbus: int | None = None
        self.enabled_at = -1e9
        self.value_at = -1e9
        self.erasing = False
        self.erase_delay = 2.0
        self.read_gap_limit: float | None = 2.5
        self.last_block_read = -1e9
        self.marker_until = -1e9
        self._shabbat_saved = False
        # compressor
        now = clock()
        self.running = False
        self.pending_at: float | None = None
        self.last_stop = now - last_stop_ago
        self.run_start: float | None = None
        self._t = now
        self._auto_dir = "cooling"
        self.slave1 = True
        self.writes: list[tuple[float, int, tuple[int, ...]]] = []
        self.starts: list[float] = []

    # -- time -----------------------------------------------------------------------------------------------------

    def advance(self) -> None:
        now = self.clock()
        dt = max(0.0, now - self._t)
        self._t = now
        if self.active and self.source == 0x2000 and now - self.enabled_at >= EXPIRY:
            self.active, self.source, self.mirror = False, 0, self.cached
        if (
            self.erasing and self.active and self.source == 0x2000 and self.mirror != 0
            and now - self.value_at >= self.erase_delay
        ):  # fmt: skip
            self.mirror = 0
        if (
            self.read_gap_limit is not None and self.active and self.source == 0x2000 and self.mirror != 0
            and now - max(self.last_block_read, self.value_at) > self.read_gap_limit
        ):  # fmt: skip
            self.mirror = 0  # not read often enough: the value is dropped
        self._compressor(now)
        cooling = self.mode in (1, 4) or (self.mode == 3 and self._auto_dir == "cooling")
        if self.running:
            self.room += (-1 if cooling else 1) * self.room_rate * dt / 60
            if self.freeze:
                self.coil = max(-25.0, self.coil - 16.0 * dt / 60)
            elif cooling:
                self.coil = max(self.coil_floor, self.coil - self.coil_rate * dt / 60)
            else:
                self.coil = min(self.heat_coil_peak, max(self.coil, self.room) + self.heat_coil_rate * dt / 60)
        else:
            self.room += (1 if self.mode != 2 else -1) * self.drift * dt / 60
            if self.coil > self.room:
                self.coil = max(self.room, self.coil - 28.0 * dt / 60)
            else:
                self.coil = min(self.room, self.coil + 3.0 * dt / 60)

    def _in_use(self) -> float:
        return self.mirror if self.active else round(self.room)

    def _demand(self, temp: float) -> str | None:
        sp = self.setpoint
        if self.mode in (1, 4):
            return "start" if temp >= sp + 1 else ("stop" if temp <= sp - 1 else None)
        if self.mode == 2:
            return "start" if temp <= sp - 1 else ("stop" if temp >= sp + 1 else None)
        if self.mode == 3:  # Auto: start in whichever direction is needed, stop past the other threshold
            if self.running:
                cooling = self._auto_dir == "cooling"
                return "stop" if (temp <= sp - 1 if cooling else temp >= sp + 1) else None
            if temp >= sp + 1 or temp <= sp - 1:
                self._auto_dir = "cooling" if temp >= sp + 1 else "heating"
                return "start"
            return None
        return "stop"

    def _compressor(self, now: float) -> None:
        if self.mode in (0, 5):
            if self.running:
                self.running, self.last_stop, self.run_start = False, now, None
            self.pending_at = None
            return
        demand = "start" if self.shabat else self._demand(self._in_use())
        if not self.running:
            if self.pending_at is None and demand == "start" and now - self.last_stop >= MIN_OFF:
                self.pending_at = now
            if self.pending_at is not None:
                if demand != "start" and not self.shabat:
                    self.pending_at = None
                elif now - self.pending_at >= 3.0:
                    self.running, self.run_start, self.pending_at = True, now, None
                    self.starts.append(now)
        elif demand == "stop" and now - self.run_start >= MIN_RUN and not self.shabat:
            self.running, self.last_stop, self.run_start = False, now, None

    # -- registers ------------------------------------------------------------------------------------------------

    def note_block_read(self) -> None:
        self.advance()
        self.last_block_read = self.clock()

    def timer_bit(self) -> bool:
        if self.mode == 2:
            return False  # never set in Heat (2026-10-03)
        return self.running or self.clock() - self.last_stop < TIMER

    def reg_3307(self) -> int:
        if self.active and self.source == 0x2000 and self.mirror != 0 and self.last_modbus is not None:
            return self.last_modbus
        return self.cached

    def regs160(self) -> list[int]:
        regs = [0] * 19
        regs[0], regs[1], regs[2], regs[3] = self.mode, self.fan, self.setpoint, round(self.room)
        regs[4], regs[5] = self.idu_fault, 1
        regs[6], regs[7] = self.flag, self.reg_3307()
        regs[10] = 160
        regs[11] = 0x0A if self.shabat else 0
        regs[17] = 0x0A
        regs[18] = 0x0A if self.sleep else 0
        return regs

    def regs4801(self) -> list[int]:
        c4801 = MODE_BITS.get(self.mode, 0) | (0x4000 if self.active else 0) | (0x0004 if self.shabat else 0)
        c4803 = (self.source if self.active else 0) | (0x0080 if self.shabat else 0)
        if self.mode == 0:
            word = 0x0204
        elif self.running:
            word = 0x9704
        elif self.pending_at is not None:
            word = 0x1304
        else:
            word = 0x0304
        if self.mode != 0 and self.timer_bit():
            word |= 0x0400
        if self.mode == 2 or (self.mode == 3 and self._auto_dir == "heating"):
            word |= 0x0800
        regs = [0] * 11
        regs[0], regs[2], regs[4] = c4801, c4803, word
        regs[8] = (max(0, min(255, int(self.mirror))) << 8) | (self.fan & 3)
        regs[10] = ((int(self.coil) & 0xFF) << 8) | round(self.room)
        return regs

    def marker(self) -> list[int]:
        return [0x4828, 0, 0x4001] if self.clock() < self.marker_until else [0xFFFF] * 3

    # -- writes ---------------------------------------------------------------------------------------------------

    def write(self, address: int, values: tuple[int, ...] | list[int]) -> None:
        now = self.clock()
        self.advance()
        values = tuple(values)
        self.writes.append((now, address, values))
        if address == 0x3306 and len(values) == 2:  # the enable block [1, value]
            self.flag = 1
            self._modbus_value(values[1], now, enable=True)
            return
        if address == 0x3306:
            if values[0] == 1:
                self.flag = 1
                if self.active and self.source == 0x2000:
                    self.enabled_at = now  # keep-alive: re-arms the timer, the value is not disturbed
                else:
                    self.active, self.source, self.enabled_at = True, 0x2000, now
                    self.mirror = self.last_modbus if self.last_modbus is not None else 0
            else:
                self.flag, self.active, self.source, self.mirror = 0, False, 0, self.cached
            return
        if address == 0x3307:
            if self.flag == 1:
                self._modbus_value(values[0], now, enable=not (self.active and self.source == 0x2000))
            return
        if address == 0x3300:
            for i, v in enumerate(values):
                setattr(self, ("mode", "fan", "setpoint")[i], v)
            return
        if address == 0x3301:
            self.fan = values[0]
        elif address == 0x3302:
            self.setpoint = values[0]
        elif address == 0x330B:
            self.set_shabat(values[0] == 0x0A)
        elif address == 0x3312:
            self.sleep = values[0] == 0x0A

    def _modbus_value(self, value: int, now: float, enable: bool) -> None:
        if enable:
            self.enabled_at = now
        self.active, self.source, self.mirror = True, 0x2000, value
        self.last_modbus, self.value_at = value, now

    def set_shabat(self, on: bool) -> None:
        if on and not self.shabat:
            self._shabbat_saved = self.active and self.source == 0x2000
            self.active, self.source, self.mirror = False, 0, self.cached
        elif not on and self.shabat and self._shabbat_saved:
            self.flag, self.active, self.source = 1, True, 0x2000
            self.mirror = self.last_modbus if self.last_modbus is not None else 0
            self.enabled_at = self.clock()
        self.shabat = on

    # -- the remote -----------------------------------------------------------------------------------------------

    def remote_press(self, *, mode: int | None = None, fan: int | None = None, setpoint: int | None = None) -> None:
        self.advance()
        self.marker_until = self.clock() + 3
        if self.active:
            self.active, self.source, self.mirror = False, 0, self.cached
        if mode is not None:
            self.mode = mode
        if fan is not None:
            self.fan = fan
        if setpoint is not None:
            self.setpoint = setpoint

    def remote_ifeel(self, temp: int) -> None:
        self.advance()
        self.marker_until = self.clock() + 3
        self.cached = temp
        self.active, self.source, self.mirror = True, 0x1000, temp
