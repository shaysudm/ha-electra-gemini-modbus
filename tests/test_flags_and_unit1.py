# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shabbat/sleep switches, timer/IFeel sensors, and a unit 1 that is missing or laid out differently."""
import asyncio

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE

from custom_components.electra_gemini import coordinator as coordinator_module
from test_integration import CLIMATE, _defaults, fast_writes, setup, state  # noqa: F401 - fixtures

SHABAT = "switch.ac_modbus_shabbat_mode"
SLEEP = "switch.ac_modbus_sleep_mode"
TIMER = "binary_sensor.ac_modbus_timer_active"
IFEEL = "sensor.ac_modbus_ifeel_source"
UNIT1_ENTITIES = (
    "sensor.ac_modbus_compressor_state",
    "binary_sensor.ac_modbus_compressor_running",
    "sensor.ac_modbus_coil_temperature",
)


async def switch(hass, service, entity_id):
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)


# -- Shabbat and sleep -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("entity_id, address", [(SHABAT, 0x330B), (SLEEP, 0x3312)])
async def test_switch_writes_0x0a_to_turn_on_and_0_to_turn_off(hass, setup, sim, entity_id, address):
    assert state(hass, entity_id).state == STATE_OFF
    await switch(hass, "turn_on", entity_id)
    assert [(w.fc, w.address, w.values) for w in sim.writes] == [(6, address, [0x0A])]
    assert state(hass, entity_id).state == STATE_ON
    await switch(hass, "turn_off", entity_id)
    assert [(w.fc, w.address, w.values) for w in sim.writes][1:] == [(6, address, [0])]
    assert state(hass, entity_id).state == STATE_OFF
    assert state(hass, CLIMATE).state == "cool"  # nothing else touched
    assert [sim.regs[a] for a in (0x3300, 0x3301, 0x3302)] == [1, 1, 24]


async def test_shabat_readback_waits_for_its_slow_register(hass, setup, sim):
    sim.shabat_lag = 0.15  # about 3.6 s on the real unit
    await switch(hass, "turn_on", SHABAT)
    assert state(hass, SHABAT).state == STATE_ON


async def test_switch_shows_what_the_unit_reports_not_what_was_asked(hass, setup, sim, monkeypatch):
    monkeypatch.setattr(coordinator_module, "READBACK_DELAYS", (0.05, 0.05))
    sim.write = lambda unit, fc, address, values: None  # acknowledged but not applied
    await switch(hass, "turn_on", SLEEP)
    assert state(hass, SLEEP).state == STATE_OFF


async def test_switch_on_when_already_on_writes_nothing(hass, setup, sim):
    sim.set_registers(shabat=0x0A)
    await setup.runtime_data.async_refresh()
    assert state(hass, SHABAT).state == STATE_ON
    await switch(hass, "turn_on", SHABAT)
    assert sim.writes == []


async def test_flags_can_be_set_while_the_ac_is_off(hass, setup, sim):
    sim.set_registers(mode=0)
    await setup.runtime_data.async_refresh()
    await switch(hass, "turn_on", SHABAT)
    assert [(w.address, w.values) for w in sim.writes] == [(0x330B, [0x0A])]
    assert state(hass, SHABAT).state == STATE_ON and state(hass, CLIMATE).state == "off"


async def test_climate_and_flag_changes_made_together_go_in_one_batch(hass, setup, sim):
    await asyncio.gather(
        hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 26}, blocking=True),
        switch(hass, "turn_on", SLEEP),
    )
    assert [(w.address, w.values) for w in sim.writes] == [(0x3302, [26]), (0x3312, [0x0A])]
    assert state(hass, SLEEP).state == STATE_ON
    assert state(hass, CLIMATE).attributes["temperature"] == 26


async def test_timer_and_ifeel_source_sensors(hass, setup, sim):
    assert state(hass, TIMER).state == STATE_OFF and state(hass, IFEEL).state == "off"
    sim.set_registers(timer=0x0A)
    sim.ifeel_active, sim.ifeel_source = True, 0x2000
    await setup.runtime_data.async_refresh()
    assert state(hass, TIMER).state == STATE_ON and state(hass, IFEEL).state == "home_assistant"
    sim.ifeel_source, sim.mirror = 0x1000, 25  # the remote's own IFeel
    await setup.runtime_data.async_refresh()
    assert state(hass, IFEEL).state == "remote"
    assert state(hass, "sensor.ac_modbus_remote_ifeel_temperature").state == "25"
    sim.ifeel_active = False
    await setup.runtime_data.async_refresh()
    assert state(hass, IFEEL).state == "off"
    assert state(hass, "sensor.ac_modbus_remote_ifeel_temperature").state == STATE_UNAVAILABLE


# -- unit 1 missing or different -------------------------------------------------------------------------------------


def break_unit1(sim, how):
    if how == "no_reply":
        sim.internal_silent = True
    elif how == "illegal_address":
        sim.internal_error = 2
    elif how == "illegal_function":
        sim.internal_error = 1
    elif how == "other_layout":
        sim.internal_cells = {0x480B: (40 << 8) | 99}  # low byte is not the room temperature


def repair_unit1(sim):
    sim.internal_silent, sim.internal_error, sim.internal_cells = False, None, {}


@pytest.mark.parametrize("how", ["no_reply", "illegal_address", "illegal_function", "other_layout"])
async def test_unusable_unit1_only_makes_its_own_entities_unavailable(hass, setup, sim, how, caplog):
    coord = setup.runtime_data
    break_unit1(sim, how)
    await coord.async_refresh()
    assert coord.last_update_success
    for entity_id in UNIT1_ENTITIES:
        assert state(hass, entity_id).state == STATE_UNAVAILABLE
    assert state(hass, CLIMATE).state == "cool"
    assert "hvac_action" not in state(hass, CLIMATE).attributes
    for entity_id in (SHABAT, SLEEP, TIMER, IFEEL, "binary_sensor.ac_modbus_alarm", "sensor.ac_modbus_odu_fault"):
        assert state(hass, entity_id).state != STATE_UNAVAILABLE
    assert "not usable" in caplog.text

    # control still works
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 23}, blocking=True)
    assert state(hass, CLIMATE).attributes["temperature"] == 23

    repair_unit1(sim)
    await asyncio.sleep(0.15)  # the reconnect wait after a timeout (0.1 s in the tests)
    await coord.async_refresh()
    assert state(hass, "sensor.ac_modbus_compressor_state").state == "ready"


@pytest.mark.parametrize(
    "mode, compressor, coil, plausible",
    [
        (1, "running", 14, True),
        (1, "running", 30, False),  # cooling, but the coil is 6 °C warmer than the room
        (1, "ready", 30, True),  # idle: no direction
        (4, "running", 30, True),  # Dry is not checked
        (5, "ready", 81, False),  # above 80
        (2, "running", 62, True),  # Heat: up to about 62 °C is normal
        (2, "running", 18, True),  # Heat: no room comparison (the board reads the unit's own warm air)
    ],
)
async def test_implausible_coil_only_makes_the_coil_entity_unavailable(
    hass, setup, sim, caplog, mode, compressor, coil, plausible
):
    coord = setup.runtime_data
    sim.set_registers(mode=mode)
    sim.compressor, sim.coil_temp = compressor, coil
    await coord.async_refresh()
    coil_state = state(hass, "sensor.ac_modbus_coil_temperature").state
    assert coil_state == (str(coil) if plausible else STATE_UNAVAILABLE)
    assert state(hass, "sensor.ac_modbus_compressor_state").state != STATE_UNAVAILABLE
    assert state(hass, "binary_sensor.ac_modbus_compressor_running").state != STATE_UNAVAILABLE
    assert ("not plausible" in caplog.text) is not plausible


async def test_implausible_coil_is_logged_once_and_recovers(hass, setup, sim, caplog):
    coord = setup.runtime_data
    sim.coil_temp = 90
    for _ in range(3):
        await coord.async_refresh()
    assert caplog.text.count("not plausible") == 1
    sim.coil_temp = 20
    await coord.async_refresh()
    assert state(hass, "sensor.ac_modbus_coil_temperature").state == "20"


async def test_unit1_missing_at_startup(hass, sim, _defaults):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.electra_gemini.const import DOMAIN

    sim.internal_error = 2
    entry = MockConfigEntry(domain=DOMAIN, title="AC Modbus", data={"host": "127.0.0.1", "port": sim.port})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert state(hass, CLIMATE).state == "cool"
    for entity_id in UNIT1_ENTITIES:
        assert state(hass, entity_id).state == STATE_UNAVAILABLE
    from homeassistant.helpers import device_registry as dr

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.model == "GEMINI IDU" and device.serial_number is None and device.hw_version is None  # left out
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_failing_unit1_is_retried_only_every_few_minutes(hass, setup, sim, caplog):
    coord = setup.runtime_data
    sim.internal_error = 2
    for _ in range(3):
        await coord.async_refresh()
    unit1_reads = sum(1 for r in sim.requests if r[0] == 1)
    for _ in range(4):
        await coord.async_refresh()
    assert sum(1 for r in sim.requests if r[0] == 1) == unit1_reads  # not asked again for now
    assert coord.last_update_success
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "Internal status cells" in r.message]
    assert len(warnings) == 1  # logged once, not on every poll

    sim.internal_error = None
    coord._internal_retry_at = hass.loop.time() - 1  # noqa: SLF001 - the retry interval (5 min) has passed
    await coord.async_refresh()
    assert sum(1 for r in sim.requests if r[0] == 1) == unit1_reads + 1
    assert state(hass, "sensor.ac_modbus_compressor_state").state == "ready"
    assert "usable again" in caplog.text
