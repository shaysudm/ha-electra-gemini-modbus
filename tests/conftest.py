# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures: the simulator as a real TCP server, and the custom component on the path."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    # The Home Assistant test plugin blocks sockets, but on Windows the event loop itself needs a loopback socket pair
    # and the simulator is a real TCP server on localhost, so switch the blocking off on the development PC.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "simulator"))

from ac_simulator import AcSimulator  # noqa: E402


@pytest.fixture
async def sim():
    simulator = AcSimulator()
    simulator.port = await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture(autouse=True)
def _enable_custom_integrations(request):
    # Only the Home Assistant tests need this (and the hass fixture); the plain tests run without it.
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
