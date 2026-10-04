# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""READ-ONLY poll of the real AC through the integration's own client (function code 3 only, nothing is written).

Usage: python tools/poll_real_ac.py HOST [PORT] [POLLS] [INTERVAL_S]
Stop any other script that talks to the EW11 first: it accepts at most 3 TCP clients.
"""
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "electra_gemini"


def load(name):
    """Load a module of the component without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(f"eg_{name}", PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"eg_{name}"] = module
    spec.loader.exec_module(module)
    return module


async def main() -> None:
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8899
    polls = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    interval = float(sys.argv[4]) if len(sys.argv) > 4 else 10
    modbus, regs = load("modbus"), load("registers")
    client = modbus.ModbusTcpClient(host, port, write_unit=160)
    try:
        for i in range(polls):
            t = time.monotonic()
            status = regs.parse_status(await client.read_holding_registers(160, regs.STATUS_ADDRESS, regs.STATUS_COUNT))
            try:
                cells = await client.read_holding_registers(1, regs.INTERNAL_ADDRESS, regs.INTERNAL_COUNT)
                internal = regs.parse_internal(cells, status.room_temp, status.mode)
            except (modbus.ModbusError, ValueError) as err:
                internal = f"unit 1 unavailable: {err}"
            print(f"poll {i + 1} ({time.monotonic() - t:.2f} s): {status}\n         {internal}")
            if i + 1 < polls:
                await asyncio.sleep(interval)
    finally:
        await client.close()


asyncio.run(main())
