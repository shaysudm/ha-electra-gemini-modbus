# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Simulator of the Electra/Airwell GEMINI IDU behind an Elfin EW11 (Modbus TCP server). No Home Assistant needed.

Reproduces what was measured on the real unit (docs/REGISTER_MAP.md):
* unit 160: registers 0x3300..0x3312; writes to 0x3300..0x3302 and the Shabbat/sleep flags (0x0A or 0; 1 is
  acknowledged but ignored); a read is validated on its last
  address only; at most 22 registers per read, more gets NO reply; an unknown unit gets no reply;
* unit 1: read only; live cells 0x47FF..0x4853 incl. compressor word 0x4805 and coil/room 0x480B; writes get exception 2;
* function codes 3, 6, 16 only (others: exception 1); writes are acknowledged (echo);
* at most 3 TCP clients (a 4th connection is closed at once).

Test hooks: ``drop_next`` (swallow N requests, i.e. a timeout), ``late_reply_next`` (answer the next request only after
a delay, simulating a late reply), ``latency``, ``writes`` (log of every accepted write), ``compressor`` (state name),
``set_registers``. Run standalone:  python ac_simulator.py [--host 127.0.0.1] [--port 8899]
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import time
from dataclasses import dataclass, field

MAX_READ = 22
UNIT_CONTROL = 160
UNIT_INTERNAL = 1
MAX_CLIENTS = 3


# 0x4040-0x4059 laid out as on the real unit, with made-up serial numbers: "0A0B12345678", FFFF FFFF, "1A0058", spaces,
# " 009", "1234567890", "857071" (two characters per register, the low byte first)
IDENTITY = dict(zip(range(0x4040, 0x405A), [
    0x4130, 0x4230, 0x3231, 0x3433, 0x3635, 0x3837, 0xFFFF, 0xFFFF, 0x4131, 0x3030, 0x3835, 0x2020, 0x2020, 0x2020,
    0x2020, 0x2020, 0x3020, 0x3930, 0x3231, 0x3433, 0x3635, 0x3837, 0x3039, 0x3538, 0x3037, 0x3137,
]))  # fmt: skip


@dataclass
class WriteRecord:
    unit: int
    fc: int
    address: int
    values: list[int]
    compressor: str
    mode: int


@dataclass
class AcSimulator:
    unit_control: int = UNIT_CONTROL
    latency: float = 0.0
    apply_lag: float = 0.0  # seconds until a write shows in the registers (the real unit needs a moment)
    shabat_lag: float = 0.0  # the same for the Shabbat flag (about 3.6 s on the real unit)
    internal_cells: dict[int, int] = field(default_factory=dict)  # unit-1 overrides, e.g. to simulate another layout
    internal_error: int | None = None  # unit 1: answer every read with this exception code
    internal_silent: bool = False  # unit 1: do not answer at all
    # IFeel (see unit_model.py for the full model used by the controller tests; this is the register-level part)
    ifeel_active: bool = False
    ifeel_source: int = 0
    mirror: int = 18
    cached_remote: int = 18
    last_modbus: int | None = None
    erase_values: bool = False  # every IFeel value written is dropped at once (an unexplained loss, for tests)
    no_modbus_ifeel: bool = False  # like board 1A0040: IFeel goes on with the remote's source and cached value
    read_gap_limit: float | None = 2.5  # a Modbus IFeel value is dropped unless slave 160 is read in blocks this often
    last_block_read: float = 0.0
    value_at: float = 0.0
    ffff_next: int = 0  # answer the next N reads with all 0xFFFF (seen with two TCP clients)
    marker_until: float = 0.0
    regs: dict[int, int] = field(default_factory=dict)  # unit 160 registers
    compressor: str = "ready"  # ready | lockout | pre_start | running
    coil_temp: int = 23  # signed (encoded as a signed byte)
    auto_heating: bool = False  # Auto chose heating: 0x4805 bit 11 set
    drop_next: int = 0
    late_reply_next: float | None = None  # seconds; the next request is answered late
    inject_stale: bool = False  # send a reply with an old transaction id before the next real reply
    writes: list[WriteRecord] = field(default_factory=list)
    requests: list[tuple[int, int, int, int]] = field(default_factory=list)  # unit, fc, address, count/value
    connections: int = 0

    def __post_init__(self) -> None:
        self.regs = {
            0x3300: 1, 0x3301: 1, 0x3302: 24, 0x3303: 24, 0x3304: 0, 0x3305: 1, 0x3306: 0, 0x3307: 18,
            0x330A: 160, 0x330B: 0, 0x330E: 0, 0x330F: 0, 0x3310: 0, 0x3311: 0x0A, 0x3312: 0,
        } | self.regs
        self._server: asyncio.Server | None = None
        self._timers: list[asyncio.TimerHandle] = []
        self._writers: set[asyncio.StreamWriter] = set()

    # -- state ------------------------------------------------------------------------------------------------------

    def set_registers(self, **named: int) -> None:
        names = {"mode": 0x3300, "fan": 0x3301, "setpoint": 0x3302, "room": 0x3303, "idu_fault": 0x3304,
                 "odu_fault": 0x3305, "ifeel": 0x3306, "shabat": 0x330B, "timer": 0x330E, "defrost": 0x330F,
                 "overflow": 0x3310, "alarm": 0x3311, "sleep": 0x3312}
        for name, value in named.items():
            self.regs[names[name]] = value

    def compressor_word(self) -> int:
        """0x4805 as measured: Standby 0x0204; Cool ready 0x0304, lockout 0x0704, pre-start 0x1304, running 0x9704;
        Dry has bit 8 clear; Fan never has the compressor."""
        mode = self.regs[0x3300]
        if mode == 0:
            return 0x0204
        comp = "ready" if mode == 5 else self.compressor
        word = {"ready": 0x0304, "lockout": 0x0704, "pre_start": 0x1304, "running": 0x9704}[comp]
        if mode == 4:
            word &= ~0x0100
        if mode == 2 or (mode == 3 and self.auto_heating):
            word |= 0x0800  # bit 11: the heating side
        return word

    def _drop_check(self) -> None:
        now = time.monotonic()
        if (
            self.read_gap_limit is not None and self.ifeel_active and self.ifeel_source == 0x2000 and self.mirror != 0
            and now - max(self.last_block_read, self.value_at) > self.read_gap_limit
        ):  # fmt: skip
            self.mirror = 0
            self.regs[0x3307] = self.cached_remote

    def read(self, unit: int, address: int, count: int) -> list[int] | int | None:
        """Register values, a Modbus exception code, or None for no reply."""
        if count > MAX_READ or count < 1:
            return None
        self._drop_check()
        if self.ffff_next > 0:
            self.ffff_next -= 1
            return [0xFFFF] * count
        if unit == self.unit_control and count >= 8:
            self.last_block_read = time.monotonic()
        last = address + count - 1
        if unit == self.unit_control:
            if not (0x3300 <= last <= 0x3312 or last == 0x310E):
                return 2
            return [self.regs.get(a, 0) for a in range(address, address + count)]
        if unit == UNIT_INTERNAL:
            if self.internal_silent:
                return None
            if self.internal_error is not None:
                return self.internal_error
            if not (0x4000 <= last <= 0x46FF or 0x47FF <= last <= 0x4853):
                return 2
            room = self.regs[0x3303]
            mode = self.regs[0x3300]
            shabat = self.regs[0x330B] == 0x0A
            cells = {
                0x4801: {1: 0x0800, 2: 0x0100, 3: 0x1000, 4: 0x0400, 5: 0x0200}.get(mode, 0)
                | (0x4000 if self.ifeel_active else 0)
                | (0x0004 if shabat else 0),
                0x4803: (self.ifeel_source if self.ifeel_active else 0) | (0x0080 if shabat else 0),
                0x4805: self.compressor_word(),
                0x4809: (self.mirror << 8) | (self.regs[0x3301] & 3),
                0x480B: ((self.coil_temp & 0xFF) << 8) | (room & 0xFF),
            }
            marker = time.monotonic() < self.marker_until
            for a in (0x4850, 0x4851, 0x4852):
                cells[a] = (0x4828, 0, 0x4001)[a - 0x4850] if marker else 0xFFFF
            cells |= IDENTITY  # the identity block as logged on the real unit
            cells |= self.internal_cells
            # filler as on the real unit: 0xFFFF in the erased area 0x40D9-0x444B, 0 elsewhere
            return [cells.get(a, 0xFFFF if 0x40D9 <= a <= 0x444B else 0) for a in range(address, address + count)]
        return None

    def write(self, unit: int, fc: int, address: int, values: list[int]) -> int | None:
        """None = accepted, else an exception code."""
        if unit != self.unit_control:
            return 2
        if address == 0x3306 and len(values) == 2 and values[0] == 1:  # the IFeel enable block [1, value]
            self.regs[0x3306] = 1
            self._ifeel_value(values[1])
            self.writes.append(WriteRecord(unit, fc, address, list(values), self.compressor, self.regs[0x3300]))
            return None
        if len(values) == 1 and address == 0x3306:
            if values[0] == 1:
                self.regs[0x3306] = 1
                if not (self.ifeel_active and self.ifeel_source == 0x2000):
                    self.ifeel_active, self.ifeel_source = True, 0x2000
                    self.mirror = self.last_modbus if self.last_modbus is not None else 0
                    self.value_at = time.monotonic()
            elif values[0] == 0:
                self.regs[0x3306] = 0
                self.ifeel_active, self.ifeel_source, self.mirror = False, 0, self.cached_remote
            self.writes.append(WriteRecord(unit, fc, address, list(values), self.compressor, self.regs[0x3300]))
            return None
        if len(values) == 1 and address == 0x3307:
            if self.regs[0x3306] == 1:
                self._ifeel_value(values[0])
            self.writes.append(WriteRecord(unit, fc, address, list(values), self.compressor, self.regs[0x3300]))
            return None
        if len(values) == 1 and address in (0x330B, 0x3312):
            # Shabbat / sleep flag: 0x0A = on, 0 = off; any other value is acknowledged but ignored (as measured).
            # The Shabbat register follows the write only after about 3.6 s on the real unit (shabat_lag).
            if values[0] in (0, 0x0A):
                self._schedule(address, values, self.shabat_lag if address == 0x330B else self.apply_lag)
            self.writes.append(WriteRecord(unit, fc, address, list(values), self.compressor, self.regs[0x3300]))
            return None
        if not (0x3300 <= address and address + len(values) - 1 <= 0x3302):
            return 2
        checks = [(0x3300, 0, 5), (0x3301, 0, 5), (0x3302, 16, 30)]
        for offset, value in enumerate(values):
            _, lo, hi = checks[address + offset - 0x3300]
            if not lo <= value <= hi:
                return 3
        self._schedule(address, values, self.apply_lag)
        self.writes.append(WriteRecord(unit, fc, address, list(values), self.compressor, self.regs[0x3300]))
        return None

    def _schedule(self, address: int, values: list[int], lag: float) -> None:
        if lag > 0:  # acknowledged now, visible in the registers only after a while
            values = list(values)
            self._timers.append(asyncio.get_running_loop().call_later(lag, self._apply, address, values))
        else:
            self._apply(address, values)

    def _ifeel_value(self, value: int) -> None:
        if self.no_modbus_ifeel:
            self.ifeel_active, self.ifeel_source, self.mirror = True, 0x1000, self.cached_remote
            self.regs[0x3307] = value
            return
        self._drop_check()
        self.value_at = time.monotonic()
        self.last_modbus = value
        self.ifeel_active, self.ifeel_source = True, 0x2000
        self.mirror = 0 if self.erase_values else value
        self.regs[0x3307] = self.cached_remote if self.erase_values else value

    def remote_press(self, *, mode: int | None = None, fan: int | None = None, setpoint: int | None = None) -> None:
        """A button on the remote: IFeel off inside the unit (0x3306 keeps reading 1), the settings change."""
        self.marker_until = time.monotonic() + 3
        self.ifeel_active, self.ifeel_source, self.mirror = False, 0, self.cached_remote
        self.regs[0x3307] = self.cached_remote
        for name, value in (("mode", mode), ("fan", fan), ("setpoint", setpoint)):
            if value is not None:
                self.set_registers(**{name: value})

    def _apply(self, address: int, values: list[int]) -> None:
        for offset, value in enumerate(values):
            self.regs[address + offset] = value

    # -- MBAP server ------------------------------------------------------------------------------------------------

    def handle_pdu(self, unit: int, pdu: bytes) -> bytes | None:
        fc = pdu[0]
        if fc == 3 and len(pdu) == 5:
            address, count = struct.unpack(">HH", pdu[1:])
            self.requests.append((unit, fc, address, count))
            result = self.read(unit, address, count)
            if result is None:
                return None
            if isinstance(result, int):
                return bytes([fc | 0x80, result])
            return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *result)
        if fc == 6 and len(pdu) == 5:
            address, value = struct.unpack(">HH", pdu[1:])
            self.requests.append((unit, fc, address, value))
            error = self.write(unit, fc, address, [value])
            return bytes([fc | 0x80, error]) if error else pdu
        if fc == 16 and len(pdu) >= 6:
            address, count, nbytes = struct.unpack(">HHB", pdu[1:6])
            if nbytes != 2 * count or len(pdu) != 6 + nbytes:
                return bytes([fc | 0x80, 3])
            values = list(struct.unpack(f">{count}H", pdu[6:]))
            self.requests.append((unit, fc, address, count))
            error = self.write(unit, fc, address, values)
            return bytes([fc | 0x80, error]) if error else pdu[:5]
        if unit not in (UNIT_CONTROL, self.unit_control, UNIT_INTERNAL):
            return None
        return bytes([fc | 0x80, 1])  # illegal function

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connections >= MAX_CLIENTS:
            writer.close()
            return
        self.connections += 1
        self._writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _proto, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                if self.drop_next > 0:
                    self.drop_next -= 1
                    continue
                reply = self.handle_pdu(unit, pdu)
                if reply is None:
                    continue
                frame = struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply
                if self.inject_stale:
                    self.inject_stale = False  # a leftover reply of an earlier request (older transaction id)
                    writer.write(struct.pack(">HHHB", (tid - 1) & 0xFFFF, 0, 5, unit) + bytes([3, 2, 0, 99]))
                if self.late_reply_next is not None:
                    delay, self.late_reply_next = self.late_reply_next, None
                    asyncio.get_running_loop().call_later(delay, _safe_write, writer, frame)
                    continue
                if self.latency:
                    await asyncio.sleep(self.latency)
                writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.connections -= 1
            self._writers.discard(writer)
            writer.close()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        for writer in list(self._writers):
            writer.transport.abort()  # do not wait for clients a failed test left connected
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()  # 3.12+: waits for the clients; the caller closes them first


def _safe_write(writer: asyncio.StreamWriter, frame: bytes) -> None:
    if not writer.is_closing():
        writer.write(frame)


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    sim = AcSimulator()
    port = await sim.start(args.host, args.port)
    print(f"Simulated Electra GEMINI on {args.host}:{port} (Ctrl+C to stop)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
