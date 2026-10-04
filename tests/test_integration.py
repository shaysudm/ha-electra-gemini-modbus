# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The integration inside a test Home Assistant, talking to the simulator over TCP."""
import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.electra_gemini import coordinator as coordinator_module
from custom_components.electra_gemini.const import DOMAIN

CLIMATE = "climate.ac_modbus"


@pytest.fixture(autouse=True)
def fast_writes(monkeypatch):
    monkeypatch.setattr(coordinator_module, "MIN_WRITE_INTERVAL", 0.05)
    monkeypatch.setattr(coordinator_module, "READBACK_DELAYS", (0.05,) * 6)


@pytest.fixture
def _defaults(monkeypatch):
    # The client's reconnect wait after a timeout is 2 s by default, too slow for tests
    from custom_components.electra_gemini import modbus

    original = modbus.ModbusTcpClient.__init__

    def init(self, *a, **k):
        k.update(timeout=0.3, reconnect_wait=0.1)
        original(self, *a, **k)

    monkeypatch.setattr(modbus.ModbusTcpClient, "__init__", init)


@pytest.fixture
async def setup(hass: HomeAssistant, sim, _defaults):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AC Modbus",
        # "unit_id" is what the first version stored; it must be ignored now (the unit ID is fixed at 160)
        data={"host": "127.0.0.1", "port": sim.port, "unit_id": 5},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def state(hass, entity_id):
    return hass.states.get(entity_id)


async def test_initial_state(hass, setup, sim):
    s = state(hass, CLIMATE)
    assert s.state == "cool"
    assert s.attributes["current_temperature"] == 24
    assert s.attributes["temperature"] == 24
    assert s.attributes["fan_mode"] == "medium"
    assert s.attributes["hvac_action"] == "idle"
    assert set(s.attributes["hvac_modes"]) == {"off", "cool", "heat", "dry", "fan_only"}  # no Auto (2026-10-04)
    assert s.attributes["min_temp"] == 16 and s.attributes["max_temp"] == 30
    assert state(hass, "sensor.ac_modbus_compressor_state").state == "ready"
    assert state(hass, "binary_sensor.ac_modbus_compressor_running").state == STATE_OFF
    assert state(hass, "sensor.ac_modbus_coil_temperature").state == "23"
    # the definition, not the number, when one exists
    assert state(hass, "sensor.ac_modbus_odu_fault").state == "OOT/OCT short (outdoor/coil temp sensor)"
    assert state(hass, "sensor.ac_modbus_odu_fault").attributes["code"] == 1
    assert state(hass, "sensor.ac_modbus_idu_fault").state == "OK"
    assert state(hass, "binary_sensor.ac_modbus_alarm").state == STATE_ON
    assert state(hass, "binary_sensor.ac_modbus_odu_fault_active").state == STATE_ON
    assert state(hass, "binary_sensor.ac_modbus_idu_fault_active").state == STATE_OFF
    assert state(hass, "binary_sensor.ac_modbus_defrost").state == STATE_OFF
    assert state(hass, "binary_sensor.ac_modbus_overflow").state == STATE_OFF
    assert state(hass, "binary_sensor.ac_modbus_alarm").attributes["device_class"] == "problem"
    assert sim.writes == []  # setting up never writes


async def test_entities_are_diagnostic_and_on_one_device(hass, setup):
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup.entry_id)
    assert len(entries) == 20
    controls = {
        CLIMATE,
        "switch.ac_modbus_shabbat_mode",
        "switch.ac_modbus_sleep_mode",
        "switch.ac_modbus_ifeel_control",
        "select.ac_modbus_ifeel_room_sensor",
        "sensor.ac_modbus_ifeel_control_status",
    }
    assert {e.entity_id for e in entries if e.entity_category is None} == controls
    others = [e for e in entries if e.entity_id not in controls]
    assert all(e.entity_category.value == "diagnostic" for e in others)
    assert len({e.device_id for e in entries}) == 1


async def test_hvac_action_follows_the_compressor(hass, setup, sim):
    sim.compressor = "running"
    coord = setup.runtime_data
    await coord.async_refresh()
    assert state(hass, CLIMATE).attributes["hvac_action"] == "cooling"
    assert state(hass, "binary_sensor.ac_modbus_compressor_running").state == STATE_ON
    assert state(hass, "sensor.ac_modbus_compressor_state").state == "running"
    sim.compressor = "lockout"
    await coord.async_refresh()
    assert state(hass, "sensor.ac_modbus_compressor_state").state == "restart_lockout"


async def test_turn_off_and_on(hass, setup, sim):
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    assert [(w.fc, w.address, w.values) for w in sim.writes] == [(6, 0x3300, [0])]
    assert state(hass, CLIMATE).state == "off"
    assert state(hass, CLIMATE).attributes["hvac_action"] == "off"

    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert [(w.fc, w.address, w.values) for w in sim.writes][1:] == [(16, 0x3300, [1, 1, 24])]
    assert state(hass, CLIMATE).state == "cool"


async def test_set_hvac_mode_uses_a_block_with_current_fan_and_setpoint(hass, setup, sim):
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "dry"}, blocking=True)
    assert [(w.fc, w.address, w.values) for w in sim.writes] == [(16, 0x3300, [4, 1, 24])]
    assert state(hass, CLIMATE).state == "dry"
    assert state(hass, CLIMATE).attributes["hvac_action"] == "drying"


async def test_set_temperature_is_fc6_and_no_op_writes_nothing(hass, setup, sim):
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 25}, blocking=True)
    assert [(w.fc, w.address, w.values) for w in sim.writes] == [(6, 0x3302, [25])]
    assert state(hass, CLIMATE).attributes["temperature"] == 25
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 25}, blocking=True)
    assert len(sim.writes) == 1


async def test_set_fan_mode(hass, setup, sim):
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": CLIMATE, "fan_mode": "high"}, blocking=True)
    assert [(w.fc, w.address, w.values) for w in sim.writes] == [(16, 0x3300, [1, 2, 24])]
    assert state(hass, CLIMATE).attributes["fan_mode"] == "high"


async def test_changes_while_off_are_applied_with_the_turn_on_block(hass, setup, sim):
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 22}, blocking=True)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": CLIMATE, "fan_mode": "low"}, blocking=True)
    assert len(sim.writes) == 1  # only the switch-off
    assert state(hass, CLIMATE).attributes["temperature"] == 22
    assert state(hass, CLIMATE).attributes["fan_mode"] == "low"
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "cool"}, blocking=True)
    assert sim.writes[-1].values == [1, 0, 22]


async def test_turn_on_returns_to_the_last_active_mode(hass, setup, sim):
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "fan_only"}, blocking=True)
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert sim.writes[-1].values[0] == 5


async def test_quick_repeated_changes_are_coalesced_and_spaced(hass, setup, sim):
    import asyncio

    calls = [
        hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": t}, blocking=True)
        for t in (22, 23, 26, 27, 28)
    ]
    await asyncio.gather(*calls)
    assert len(sim.writes) <= 2  # five requests, at most two batches
    assert sim.regs[0x3302] == 28  # the latest wins


async def test_writes_only_touch_the_control_registers(hass, setup, sim):
    for service, data in (
        ("set_hvac_mode", {"hvac_mode": "heat"}),
        ("set_fan_mode", {"fan_mode": "auto"}),
        ("set_temperature", {"temperature": 30}),
        ("turn_off", {}),
        ("turn_on", {}),
    ):
        await hass.services.async_call("climate", service, {"entity_id": CLIMATE, **data}, blocking=True)
    assert sim.writes
    for w in sim.writes:
        assert w.unit == 160 and 0x3300 <= w.address and w.address + len(w.values) - 1 <= 0x3302


async def test_failed_write_raises_and_is_not_retried(hass, setup, sim):
    sim.drop_next = 100  # the AC stops answering
    before = len(sim.requests)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 26}, blocking=True)
    assert len(sim.requests) - before <= 1 and sim.writes == []


async def test_unit1_failure_only_makes_its_entities_unavailable(hass, setup, sim):
    coord = setup.runtime_data
    real = sim.read
    sim.read = lambda unit, address, count: None if unit == 1 else real(unit, address, count)
    await coord.async_refresh()
    assert coord.last_update_success
    assert state(hass, CLIMATE).state == "cool"
    assert state(hass, "sensor.ac_modbus_compressor_state").state == STATE_UNAVAILABLE
    assert state(hass, "binary_sensor.ac_modbus_compressor_running").state == STATE_UNAVAILABLE
    assert state(hass, "sensor.ac_modbus_coil_temperature").state == STATE_UNAVAILABLE
    assert state(hass, "binary_sensor.ac_modbus_alarm").state == STATE_ON
    assert "hvac_action" not in state(hass, CLIMATE).attributes  # unknown without the compressor flag
    sim.read = real
    await coord.async_refresh()
    assert state(hass, "sensor.ac_modbus_compressor_state").state == "ready"


async def test_poll_failure_makes_everything_unavailable_then_recovers(hass, setup, sim):
    coord = setup.runtime_data
    sim.drop_next = 5
    await coord.async_refresh()
    assert not coord.last_update_success
    for entity_id in (CLIMATE, "sensor.ac_modbus_odu_fault", "binary_sensor.ac_modbus_alarm"):
        assert state(hass, entity_id).state == STATE_UNAVAILABLE
    sim.drop_next = 0
    await coord.async_refresh()
    import asyncio

    await asyncio.sleep(0.2)
    await coord.async_refresh()
    assert state(hass, CLIMATE).state == "cool"


async def test_options_change_the_polling_interval(hass, setup):
    from datetime import timedelta

    assert setup.runtime_data.update_interval == timedelta(seconds=10)
    hass.config_entries.async_update_entry(setup, options={"scan_interval": 30})
    await hass.async_block_till_done()
    assert setup.runtime_data.update_interval == timedelta(seconds=30)



# -- readback after a write ------------------------------------------------------------------------------------------


async def test_readback_waits_for_a_unit_that_shows_the_write_late(hass, setup, sim):
    """The unit acknowledges at once but its registers change only after a moment: the entity must still end up
    showing the new value right after the service call, not after the next poll."""
    sim.apply_lag = 0.12
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 27}, blocking=True)
    assert sim.regs[0x3302] == 27
    assert state(hass, CLIMATE).attributes["temperature"] == 27
    sim.apply_lag = 0.12
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "off"}, blocking=True)
    assert state(hass, CLIMATE).state == "off"


async def test_readback_reads_only_after_a_delay_and_repeats_until_it_matches(hass, setup, sim, monkeypatch):
    monkeypatch.setattr(coordinator_module, "READBACK_DELAYS", (0.1,) * 8)
    sim.apply_lag = 0.45
    before = len(sim.requests)
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 20}, blocking=True)
    reads = [r for r in sim.requests[before:] if r[1] == 3]
    assert len(reads) >= 4  # one to plan, then several until the value shows
    assert state(hass, CLIMATE).attributes["temperature"] == 20


async def test_readback_gives_up_after_the_delays_and_shows_what_the_unit_says(hass, setup, sim, monkeypatch, caplog):
    monkeypatch.setattr(coordinator_module, "READBACK_DELAYS", (0.05, 0.05))
    sim.apply_lag = 5  # never in time
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 20}, blocking=True)
    assert state(hass, CLIMATE).attributes["temperature"] == 24  # the unit still says 24: shown as is, not faked
    assert "did not show" in caplog.text


async def test_failed_readback_does_not_fail_the_write(hass, setup, sim):
    """The write went through; if only the read after it fails, the service call succeeds and a refresh is requested."""
    real = sim.read
    calls = {"n": 0}

    def flaky(unit, address, count):
        calls["n"] += 1
        return None if calls["n"] >= 2 else real(unit, address, count)  # the read before the write works, then silence

    sim.read = flaky
    await hass.services.async_call("climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 21}, blocking=True)
    assert [w.values for w in sim.writes] == [[21]]
    sim.read = real


# -- fault definitions -----------------------------------------------------------------------------------------------


async def test_faults_show_the_definition_or_the_number_if_there_is_none(hass, setup, sim):
    coord = setup.runtime_data
    sim.set_registers(idu_fault=21, odu_fault=42)
    await coord.async_refresh()
    assert state(hass, "sensor.ac_modbus_idu_fault").state == "Overflow protection"
    assert state(hass, "sensor.ac_modbus_idu_fault").attributes["code"] == 21
    assert state(hass, "sensor.ac_modbus_odu_fault").state == "42"  # no definition: the number
    assert state(hass, "binary_sensor.ac_modbus_idu_fault_active").state == STATE_ON
    sim.set_registers(idu_fault=0, odu_fault=0)
    await coord.async_refresh()
    assert state(hass, "sensor.ac_modbus_odu_fault").state == "OK"
    assert state(hass, "binary_sensor.ac_modbus_odu_fault_active").state == STATE_OFF


# -- Auto removed (owner decision 2026-10-04) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service, data",
    [("set_hvac_mode", {"hvac_mode": "auto"}), ("set_temperature", {"hvac_mode": "auto", "temperature": 24})],
)
async def test_auto_is_refused(hass, setup, sim, service, data):
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("climate", service, {"entity_id": CLIMATE, **data}, blocking=True)
    assert sim.writes == []


async def test_the_unit_in_auto_is_shown_and_setpoint_and_fan_are_refused(hass, setup, sim, caplog):
    from homeassistant.exceptions import ServiceValidationError

    coord = setup.runtime_data
    sim.set_registers(mode=3)
    sim.compressor, sim.auto_heating = "running", True
    await coord.async_refresh()
    await coord.async_refresh()
    s = state(hass, CLIMATE)
    assert s.state == "auto" and "auto" in s.attributes["hvac_modes"]
    assert s.attributes["hvac_action"] == "heating" and s.attributes["auto_direction"] == "heating"
    assert "not supported" in s.attributes["note"]
    assert caplog.text.count("put in Auto mode") == 1  # once per entry into Auto
    for service, data in (("set_temperature", {"temperature": 22}), ("set_fan_mode", {"fan_mode": "low"})):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("climate", service, {"entity_id": CLIMATE, **data}, blocking=True)
    assert sim.writes == []
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "cool"}, blocking=True)
    assert state(hass, CLIMATE).state == "cool" and "auto" not in state(hass, CLIMATE).attributes["hvac_modes"]


async def test_turn_on_after_auto_does_not_write_auto(hass, setup, sim):
    coord = setup.runtime_data
    sim.set_registers(mode=3)
    await coord.async_refresh()
    sim.set_registers(mode=0)
    await coord.async_refresh()
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert sim.writes[-1].values[0] == 1  # Cool (the mode before), not Auto


async def test_a_frozen_coil_is_shown(hass, setup, sim):
    sim.coil_temp = -24
    await setup.runtime_data.async_refresh()
    assert state(hass, "sensor.ac_modbus_coil_temperature").state == "-24"


# -- device info: the identity block (2026-10-04) ---------------------------------------------------------------------


async def test_device_info_shows_the_board_and_the_indoor_unit(hass, setup):
    from homeassistant.helpers import device_registry as dr

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), setup.entry_id)
    assert device.model == "GEMINI IDU" and device.model_id == "1A0058"
    assert device.serial_number == "A0B12345678"
    assert device.hw_version == "009 (indoor unit 857071, S/N 1234567890)"
