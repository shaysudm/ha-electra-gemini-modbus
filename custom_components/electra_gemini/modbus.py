# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal asyncio Modbus TCP (MBAP) client for the Elfin EW11 gateway.

Only what the integration needs: function codes 3, 6 and 16. One connection, one lock, so requests never interleave.
Behaviour that comes from testing the real unit (see docs/REGISTER_MAP.md, "Read block behaviour"):

* at most 22 registers per read (more gets no reply at all, i.e. a timeout);
* after a request gets no reply, wait about 2 s before reconnecting, otherwise a late reply
  can be matched to the next request;
* the EW11 accepts at most 3 TCP clients, so this client keeps exactly one connection.

Writes are restricted to an allow-list (check_write): mode, fan, setpoint, the Shabbat and sleep flags and the IFeel
registers (safety: nothing else is ever written).
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

MAX_READ_COUNT = 22
MAX_WRITE_COUNT = 3

FC_READ_HOLDING = 3
FC_WRITE_SINGLE = 6
FC_WRITE_MULTIPLE = 16

# Writes are only ever allowed to the control unit, and only to: mode, fan and setpoint (0x3300-0x3302, one or several
# in one request), the Shabbat (0x330B) and sleep (0x3312) flags (one register, value 0 or 0x0A only), the IFeel flag
# (0x3306, 0 or 1), the IFeel temperature (0x3307, 10-40) and the IFeel enable block [1, value] at 0x3306
# (docs/IFEEL_DESIGN.md, rule 13).
WRITE_ADDRESS_MIN = 0x3300
WRITE_ADDRESS_MAX = 0x3302
FLAG_WRITE_ADDRESSES = (0x330B, 0x3312)
FLAG_WRITE_VALUES = (0, 0x0A)
IFEEL_FLAG_ADDRESS = 0x3306
IFEEL_TEMP_ADDRESS = 0x3307
IFEEL_TEMP_MIN = 10
IFEEL_TEMP_MAX = 40


def check_write(write_unit: int, unit: int, address: int, values: list[int]) -> None:
    """Raise ValueError unless the write is on the allow-list."""
    count = len(values)
    control = 1 <= count <= MAX_WRITE_COUNT and address >= WRITE_ADDRESS_MIN and address + count - 1 <= WRITE_ADDRESS_MAX
    flag = count == 1 and address in FLAG_WRITE_ADDRESSES and values[0] in FLAG_WRITE_VALUES
    ifeel_flag = count == 1 and address == IFEEL_FLAG_ADDRESS and values[0] in (0, 1)
    ifeel_temp = count == 1 and address == IFEEL_TEMP_ADDRESS and IFEEL_TEMP_MIN <= values[0] <= IFEEL_TEMP_MAX
    ifeel_block = (
        count == 2 and address == IFEEL_FLAG_ADDRESS and values[0] == 1 and IFEEL_TEMP_MIN <= values[1] <= IFEEL_TEMP_MAX
    )
    if unit != write_unit or not (control or flag or ifeel_flag or ifeel_temp or ifeel_block):
        raise ValueError(f"write of {values} to unit {unit} 0x{address:04X} is not allowed")


class ModbusError(Exception):
    """Base class of all client errors."""


class ModbusConnectionError(ModbusError):
    """The connection could not be opened or was lost."""


class ModbusTimeout(ModbusError):
    """No reply within the timeout."""


class ModbusProtocolError(ModbusError):
    """A reply that does not make sense."""


class ModbusExceptionResponse(ModbusError):
    """The unit answered with a Modbus exception."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Modbus exception {code}")
        self.code = code


class ModbusTcpClient:
    """One persistent MBAP connection with serialised requests."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        write_unit: int,
        timeout: float = 3.0,
        connect_timeout: float = 5.0,
        reconnect_wait: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._write_unit = write_unit
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._reconnect_wait = reconnect_wait
        self._clock = clock
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._retry_after = 0.0
        self._tid = 0
        self.on_event = on_event  # connection events (connect, timeout, drop) for the debug log

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def close(self) -> None:
        async with self._lock:
            await self._drop(wait=False)

    # -- public requests ----------------------------------------------------------------------------------------

    async def read_holding_registers(self, unit: int, address: int, count: int) -> list[int]:
        if not 1 <= count <= MAX_READ_COUNT:
            raise ValueError(f"count must be 1..{MAX_READ_COUNT}")
        pdu = struct.pack(">BHH", FC_READ_HOLDING, address, count)
        body = await self._request(unit, pdu)
        if len(body) != 1 + 2 * count or body[0] != 2 * count:
            await self._protocol_error(f"bad read reply length {len(body)}")
        return list(struct.unpack(f">{count}H", body[1:]))

    async def write_register(self, unit: int, address: int, value: int) -> None:
        self._check_write(unit, address, [value])
        pdu = struct.pack(">BHH", FC_WRITE_SINGLE, address, value)
        body = await self._request(unit, pdu)
        if body != pdu[1:]:
            await self._protocol_error("write echo mismatch")

    async def write_registers(self, unit: int, address: int, values: list[int]) -> None:
        self._check_write(unit, address, values)
        pdu = struct.pack(">BHHB", FC_WRITE_MULTIPLE, address, len(values), 2 * len(values))
        pdu += struct.pack(f">{len(values)}H", *values)
        body = await self._request(unit, pdu)
        if body != struct.pack(">HH", address, len(values)):
            await self._protocol_error("write reply mismatch")

    # -- internals ----------------------------------------------------------------------------------------------

    def _check_write(self, unit: int, address: int, values: list[int]) -> None:
        check_write(self._write_unit, unit, address, values)

    async def _protocol_error(self, message: str) -> None:
        async with self._lock:
            await self._drop(wait=True)
        raise ModbusProtocolError(message)

    async def _request(self, unit: int, pdu: bytes) -> bytes:
        """Send one request and return the reply PDU without the function code."""
        fc = pdu[0]
        async with self._lock:
            await self._ensure_connected()
            assert self._reader is not None and self._writer is not None
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu
            try:
                self._writer.write(frame)
                await self._writer.drain()
                async with asyncio.timeout(self._timeout):
                    return await self._read_reply(tid, unit, fc)
            except TimeoutError as err:
                self._event("timeout", f"unit {unit} fc {fc}")
                await self._drop(wait=True)
                raise ModbusTimeout(f"no reply from unit {unit}") from err
            except ModbusExceptionResponse:
                raise  # a valid reply, the connection is fine
            except ModbusProtocolError:
                await self._drop(wait=True)
                raise
            except (OSError, asyncio.IncompleteReadError) as err:
                self._event("connection_lost", repr(err))
                await self._drop(wait=True)
                raise ModbusConnectionError(f"connection lost: {err!r}") from err

    async def _read_reply(self, tid: int, unit: int, fc: int) -> bytes:
        assert self._reader is not None
        while True:
            header = await self._reader.readexactly(7)
            rtid, proto, length, runit = struct.unpack(">HHHB", header)
            if proto != 0 or not 2 <= length <= 254:
                raise ModbusProtocolError(f"bad MBAP header {header.hex()}")
            body = await self._reader.readexactly(length - 1)
            if rtid != tid or runit != unit:
                _LOGGER.debug("Discarding stale reply tid=%s unit=%s", rtid, runit)
                continue
            if body[0] == fc | 0x80:
                if len(body) < 2:
                    raise ModbusProtocolError("short exception reply")
                raise ModbusExceptionResponse(body[1])
            if body[0] != fc:
                raise ModbusProtocolError(f"unexpected function code {body[0]}")
            return body[1:]

    async def _ensure_connected(self) -> None:
        if self._writer is not None:
            return
        delay = self._retry_after - self._clock()
        if delay > 0:
            self._event("reconnect_wait", f"{delay:.1f} s")
            await asyncio.sleep(delay)
        try:
            async with asyncio.timeout(self._connect_timeout):
                self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        except (OSError, TimeoutError) as err:
            self._event("connect_failed", repr(err))
            self._retry_after = self._clock() + self._reconnect_wait
            raise ModbusConnectionError(f"cannot connect to {self._host}:{self._port}: {err!r}") from err
        self._event("connect", f"{self._host}:{self._port}")

    def _event(self, kind: str, detail: str) -> None:
        if self.on_event is not None:
            self.on_event(kind, detail)

    async def _drop(self, *, wait: bool) -> None:
        """Close the connection; with wait, the next connect is delayed (see module docstring)."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            self._event("disconnect", "after an error" if wait else "closed")
        if wait:
            self._retry_after = self._clock() + self._reconnect_wait
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
