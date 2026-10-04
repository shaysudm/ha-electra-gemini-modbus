# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Modbus client against the simulator (real TCP, no Home Assistant)."""
import asyncio
import time

import pytest

from custom_components.electra_gemini.modbus import (
    ModbusConnectionError,
    ModbusExceptionResponse,
    ModbusProtocolError,
    ModbusTcpClient,
    ModbusTimeout,
)


@pytest.fixture
async def client(sim):
    c = ModbusTcpClient("127.0.0.1", sim.port, write_unit=160, timeout=0.3, reconnect_wait=0.3, connect_timeout=1.0)
    yield c
    await c.close()


async def test_read_status_block(client):
    regs = await client.read_holding_registers(160, 0x3300, 19)
    assert regs[:4] == [1, 1, 24, 24] and len(regs) == 19


async def test_read_internal_cells(client, sim):
    sim.compressor = "running"
    sim.coil_temp = 13
    regs = await client.read_holding_registers(1, 0x4805, 7)
    assert regs[0] == 0x9704 and regs[6] >> 8 == 13


async def test_22_registers_ok_and_more_refused_before_sending(client, sim):
    assert len(await client.read_holding_registers(1, 0x4800, 22)) == 22
    with pytest.raises(ValueError):
        await client.read_holding_registers(1, 0x4800, 23)
    assert all(count <= 22 for *_, count in sim.requests)


async def test_invalid_last_address_is_a_modbus_exception(client):
    with pytest.raises(ModbusExceptionResponse) as err:
        await client.read_holding_registers(160, 0x3300, 20)  # ends at 0x3313
    assert err.value.code == 2
    assert (await client.read_holding_registers(160, 0x3300, 1)) == [1]  # connection still usable


async def test_no_reply_times_out_then_waits_before_reconnecting(client, sim):
    sim.drop_next = 1
    with pytest.raises(ModbusTimeout):
        await client.read_holding_registers(160, 0x3300, 1)
    assert not client.connected
    start = time.monotonic()
    assert await client.read_holding_registers(160, 0x3300, 1) == [1]
    assert time.monotonic() - start >= 0.25  # the reconnect wait (0.3 s here, 2 s by default)


async def test_late_reply_does_not_corrupt_the_next_exchange(client, sim):
    sim.late_reply_next = 0.5  # arrives after the timeout, on the old connection
    with pytest.raises(ModbusTimeout):
        await client.read_holding_registers(160, 0x3300, 3)
    await asyncio.sleep(0.6)
    assert await client.read_holding_registers(160, 0x3302, 1) == [24]


async def test_stale_reply_on_the_same_connection_is_discarded(client, sim):
    sim.inject_stale = True
    assert await client.read_holding_registers(160, 0x3302, 1) == [24]


async def test_unknown_unit_gets_no_reply(client):
    with pytest.raises(ModbusTimeout):
        await client.read_holding_registers(77, 0x3300, 1)


async def test_write_single_and_block(client, sim):
    await client.write_register(160, 0x3302, 25)
    await client.write_registers(160, 0x3300, [4, 2, 23])
    assert [sim.regs[a] for a in (0x3300, 0x3301, 0x3302)] == [4, 2, 23]
    assert [(w.fc, w.address, w.values) for w in sim.writes] == [(6, 0x3302, [25]), (16, 0x3300, [4, 2, 23])]


async def test_flag_writes(client, sim):
    await client.write_register(160, 0x330B, 0x0A)
    await client.write_register(160, 0x3312, 0x0A)
    await client.write_register(160, 0x330B, 0)
    assert (sim.regs[0x330B], sim.regs[0x3312]) == (0, 0x0A)


async def test_write_exception_is_reported(client, sim):
    with pytest.raises(ModbusExceptionResponse) as err:
        await client.write_register(160, 0x3302, 31)
    assert err.value.code == 3
    assert sim.writes == []


@pytest.mark.parametrize(
    "unit, address, values",
    [
        (1, 0x3300, [1]),  # wrong unit
        (160, 0x3306, [2]),  # IFeel flag: 0 or 1 only
        (160, 0x3307, [9]),  # IFeel temperature: 10-40 only
        (160, 0x3307, [41]),
        (160, 0x3306, [0, 25]),  # the IFeel block must enable: [1, value]
        (160, 0x3306, [1, 50]),
        (160, 0x3306, [1, 25, 0]),
        (160, 0x3308, [0]),
        (160, 0x330A, [1]),  # slave id: never
        (160, 0x330E, [0x0A]),  # timer flag: read only
        (160, 0x3312, [1]),  # sleep flag: only 0 and 0x0A
        (160, 0x330B, [0x0A, 0]),  # flags: one register at a time
        (1, 0x330B, [0x0A]),  # flag on the wrong unit
        (160, 0x4005, [0]),  # internal area
        (160, 0x3302, [1, 2]),  # runs past 0x3302
        (160, 0x3300, [1, 1, 24, 0]),  # too many
    ],
)
async def test_writes_outside_the_control_registers_are_refused_client_side(client, sim, unit, address, values):
    with pytest.raises(ValueError):
        if len(values) == 1:
            await client.write_register(unit, address, values[0])
        else:
            await client.write_registers(unit, address, values)
    assert sim.writes == [] and sim.requests == []


async def test_connection_refused():
    c = ModbusTcpClient("127.0.0.1", 1, write_unit=160, timeout=0.3, reconnect_wait=0.1, connect_timeout=1.0)
    with pytest.raises(ModbusConnectionError):
        await c.read_holding_registers(160, 0x3300, 1)


async def test_reconnects_after_the_server_drops_the_connection(sim):
    c = ModbusTcpClient("127.0.0.1", sim.port, write_unit=160, timeout=0.3, reconnect_wait=0.1)
    await c.read_holding_registers(160, 0x3300, 1)
    c._writer.transport.abort()  # noqa: SLF001 - simulate a dropped link
    with pytest.raises((ModbusConnectionError, ModbusTimeout)):
        await c.read_holding_registers(160, 0x3300, 1)
    assert await c.read_holding_registers(160, 0x3300, 1) == [1]
    await c.close()


async def test_requests_never_interleave(client, sim):
    results = await asyncio.gather(*(client.read_holding_registers(160, 0x3300 + i, 1) for i in range(5)))
    assert results == [[1], [1], [24], [24], [0]]


async def test_protocol_error_drops_the_connection(sim, monkeypatch):
    c = ModbusTcpClient("127.0.0.1", sim.port, write_unit=160, timeout=0.3, reconnect_wait=0.1)
    real = sim.handle_pdu
    monkeypatch.setattr(sim, "handle_pdu", lambda unit, pdu: bytes([3, 4, 0, 1, 0, 2]))  # wrong byte count
    with pytest.raises(ModbusProtocolError):
        await c.read_holding_registers(160, 0x3300, 1)
    assert not c.connected
    monkeypatch.setattr(sim, "handle_pdu", real)
    assert await c.read_holding_registers(160, 0x3300, 1) == [1]
    await c.close()
