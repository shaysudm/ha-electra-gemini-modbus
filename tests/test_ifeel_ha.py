# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""IFeel control inside a test Home Assistant, talking to the simulator over TCP: entities, writes, restore, reload,
notifications and the debug log. The control rules themselves are tested on a virtual clock in test_ifeel.py."""
import asyncio
from pathlib import Path

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electra_gemini import coordinator as coordinator_module
from custom_components.electra_gemini import ifeel
from custom_components.electra_gemini.const import DOMAIN
from test_integration import CLIMATE, _defaults, fast_writes, state  # noqa: F401 - fixtures

SWITCH = "switch.ac_modbus_ifeel_control"
STATUS = "sensor.ac_modbus_ifeel_control_status"
SELECT = "select.ac_modbus_ifeel_room_sensor"
BEDROOM = "sensor.bedroom"
OPTIONS = {"scan_interval": 10, "ifeel_sensors": [BEDROOM]}


@pytest.fixture(autouse=True)
def fast_ifeel(monkeypatch):
    monkeypatch.setattr(ifeel, "ENABLE_REPEAT_DELAY", 0.05)


@pytest.fixture
def notifications(monkeypatch):
    calls = []
    monkeypatch.setattr(
        coordinator_module.persistent_notification,
        "async_create",
        lambda hass, message, title=None, notification_id=None: calls.append((notification_id, message)),
    )
    return calls


def bedroom(hass, value="25.2", unit="°C"):
    hass.states.async_set(BEDROOM, value, {"unit_of_measurement": unit, "device_class": "temperature", "friendly_name": "Bedroom"})


async def add_entry(hass, sim, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN, title="AC Modbus", data={"host": "127.0.0.1", "port": sim.port}, options=options or OPTIONS
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def entry(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def switch(hass, service):
    await hass.services.async_call("switch", service, {"entity_id": SWITCH}, blocking=True)


def ifeel_writes(sim):
    return [(w.address, w.values) for w in sim.writes if w.address in (0x3306, 0x3307)]


async def test_entities_and_the_only_sensor_is_chosen(hass, entry, sim):
    assert state(hass, SWITCH).state == STATE_OFF
    assert state(hass, STATUS).state == "off"
    assert state(hass, SELECT).state == "Bedroom"
    assert state(hass, SELECT).attributes["options"] == ["Bedroom"]
    assert state(hass, CLIMATE).attributes["fan_modes"] == ["low", "medium", "high", "auto"]
    assert state(hass, "sensor.ac_modbus_board_room_temperature").state == "24"
    assert sim.writes == []


async def test_switch_on_and_off(hass, entry, sim):
    await switch(hass, "turn_on")
    assert ifeel_writes(sim) == [(0x3306, [1, 25]), (0x3307, [25])]  # 25.2 >= 24.5: "on"
    assert state(hass, SWITCH).state == STATE_ON and state(hass, STATUS).state == "active"
    climate = state(hass, CLIMATE)
    assert climate.attributes["current_temperature"] == 25.2  # the room sensor while IFeel control drives the unit
    assert climate.attributes["current_temperature_source"] == "room_sensor"
    await entry.runtime_data.async_refresh()
    assert state(hass, "sensor.ac_modbus_ifeel_source").state == "home_assistant"

    await switch(hass, "turn_off")
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    assert state(hass, SWITCH).state == STATE_OFF and state(hass, STATUS).state == "off"
    assert state(hass, CLIMATE).attributes["current_temperature"] == 24  # the board again
    assert all(w.unit == 160 for w in sim.writes)


async def test_fahrenheit_sensor_is_converted(hass, entry, sim):
    bedroom(hass, "77.36", "°F")  # 25.2 °C
    await switch(hass, "turn_on")
    assert ifeel_writes(sim)[0] == (0x3306, [1, 25])
    assert state(hass, CLIMATE).attributes["current_temperature"] == 25.2


async def test_refused_without_a_room_sensor(hass, sim, _defaults):
    entry = await add_entry(hass, sim, {"scan_interval": 10, "ifeel_sensors": []})
    with pytest.raises(HomeAssistantError, match="room sensor"):
        await switch(hass, "turn_on")
    assert state(hass, SWITCH).state == STATE_OFF and sim.writes == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_sensor_change_drives_the_value(hass, entry, sim):
    await switch(hass, "turn_on")
    bedroom(hass, "24.0")  # between the switching points: keep "on"
    await hass.async_block_till_done()
    assert ifeel_writes(sim)[-1] == (0x3307, [25])
    entry.runtime_data.ifeel._new_at -= 200  # the 180 s since the last new value are over
    bedroom(hass, "23.4")
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    assert ifeel_writes(sim)[-1] == (0x3307, [23])


async def test_remote_mode_change_switches_ifeel_control_off(hass, entry, sim):
    await switch(hass, "turn_on")
    await entry.runtime_data.async_refresh()
    sim.remote_press(mode=4)
    await entry.runtime_data.async_refresh()
    assert state(hass, SWITCH).state == STATE_OFF
    assert state(hass, STATUS).state == "stopped_remote_mode"
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]


async def test_remote_fan_change_is_taken_back(hass, entry, sim):
    await switch(hass, "turn_on")
    await entry.runtime_data.async_refresh()
    n = len(sim.writes)
    sim.remote_press(fan=0)
    await entry.runtime_data.async_refresh()
    assert state(hass, SWITCH).state == STATE_ON
    assert [(w.address, w.values) for w in sim.writes[n:]][0] == (0x3306, [1, 25])
    assert sim.ifeel_active


async def test_ac_off_from_home_assistant_suspends(hass, entry, sim):
    await switch(hass, "turn_on")
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    assert state(hass, STATUS).state == "suspended_ac_off" and state(hass, SWITCH).state == STATE_ON
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert state(hass, STATUS).state == "active"
    assert ifeel_writes(sim)[-2][0] == 0x3306 and len(ifeel_writes(sim)[-2][1]) == 2


async def test_unload_hands_back_and_a_restart_restores(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim)
    await switch(hass, "turn_on")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    n = len(sim.writes)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ifeel_writes(sim[n:] if False else sim)[-2] == (0x3306, [1, 25])  # restored with the enable sequence
    assert state(hass, SWITCH).state == STATE_ON
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_change_reloads_without_handing_back(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim)
    await switch(hass, "turn_on")
    n = len(sim.writes)
    hass.config_entries.async_update_entry(entry, options={**OPTIONS, "hot_tolerance": 0.7})
    await hass.async_block_till_done()
    after = [(w.address, w.values) for w in sim.writes[n:]]
    assert (0x3306, [0]) not in after  # no hand-back
    assert after[0] == (0x3306, [1, 25])  # the reloaded integration took over
    assert state(hass, SWITCH).state == STATE_ON
    await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_the_chosen_sensor_from_the_options_stops(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim)
    await switch(hass, "turn_on")
    hass.config_entries.async_update_entry(entry, options={"scan_interval": 10, "ifeel_sensors": []})
    await hass.async_block_till_done()
    assert state(hass, SWITCH).state == STATE_OFF
    assert state(hass, STATUS).state == "stopped_sensor_removed"
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_select_changes_the_sensor(hass, sim, _defaults):
    bedroom(hass, "25.2")
    hass.states.async_set("sensor.living", "23.0", {"unit_of_measurement": "°C", "friendly_name": "Living room"})
    entry = await add_entry(hass, sim, {"scan_interval": 10, "ifeel_sensors": [BEDROOM, "sensor.living"]})
    assert state(hass, SELECT).state == "unknown"  # two sensors offered: none chosen yet
    await hass.services.async_call("select", "select_option", {"entity_id": SELECT, "option": "Living room"}, blocking=True)
    assert state(hass, SELECT).state == "Living room"
    await switch(hass, "turn_on")
    assert ifeel_writes(sim)[0] == (0x3306, [1, 23])  # 23.0 <= 23.5: "off"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics(hass, entry):
    from custom_components.electra_gemini.diagnostics import async_get_config_entry_diagnostics

    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["entry"]["host"] == "**REDACTED**"
    assert result["ifeel"]["status"] == "off" and result["debug_log"]["recording"] is False
    # the identity without serial numbers
    assert result["identity"] == {
        "board_part": "1A0058", "board_revision": "009", "idu_product": "857071",
        "board_serial_read": True, "idu_serial_read": True,
    }
    assert "A0B12345678" not in str(result) and "1234567890" not in str(result)
    # the last 30 minutes: the polls with their raw registers
    polls = [r for r in result["history"] if r.get("phase") == "poll"]
    assert polls and "s160" in polls[-1] and "blk4801" in polls[-1]
    # the full unit-1 snapshot: every address of the scan, the serial characters masked
    snap = result["slave1_snapshot"]
    values = {int(b["address"], 16) + i: v for b in snap["blocks"] for i, v in enumerate(b.get("values", []))}
    assert not [b for b in snap["blocks"] if "error" in b]
    assert len(values) == 1877
    assert all(values[a] is None for a in list(range(0x4040, 0x4046)) + list(range(0x4052, 0x4057)))
    assert values[0x4048] == 0x4131  # the part number stays ("1A")
    assert values[0x4801] == 0x0800 and values[0x40D9] == 0xFFFF


async def test_diagnostics_without_unit1_does_not_hold_up(hass, entry, sim):
    from custom_components.electra_gemini.diagnostics import async_get_config_entry_diagnostics

    sim.internal_error = 2  # unit 1 answers every read with an exception
    result = await async_get_config_entry_diagnostics(hass, entry)
    blocks = result["slave1_snapshot"]["blocks"]
    assert len(blocks) == 6  # two failed reads and the skipped rest, in each of the two ranges
    assert all("error" in b for b in blocks)


async def test_ifeel_events_carry_the_raw_registers(hass, entry, sim):
    await switch(hass, "turn_on")
    events = [r for r in entry.runtime_data.debug.ring if '"event": "state"' in r[1]]
    assert events and '"raw"' in events[-1][1] and '"s160"' in events[-1][1]


# -- fast reads, lost values (docs/IFEEL_DESIGN.md, "Fast status reads and lost values") ---------------------------


def status_reads(sim):
    return sum(1 for unit, fc, address, count in sim.requests if unit == 160 and fc == 3 and count == 19)


async def test_fast_reads_while_ifeel_is_on_keep_the_value(hass, entry, sim):
    await switch(hass, "turn_on")
    n = status_reads(sim)
    await asyncio.sleep(4.2)
    assert status_reads(sim) - n >= 3  # about one a second (the normal poll is every 10 s)
    assert sim.ifeel_active and sim.mirror == 25 and state(hass, STATUS).state == "active"
    await switch(hass, "turn_off")
    await asyncio.sleep(1.2)
    n = status_reads(sim)
    await asyncio.sleep(2.2)
    assert status_reads(sim) == n  # stopped with IFeel control


async def test_without_reads_the_simulated_unit_drops_the_value(hass, entry, sim):
    """The simulator's rule: no block read of slave 160 for 2.5 s after a value write drops it."""
    await switch(hass, "turn_on")
    await asyncio.sleep(1.5)
    assert sim.mirror == 25  # read every second: kept
    entry.runtime_data._fast_task.cancel()  # no more reads
    await asyncio.sleep(3.0)
    sim._drop_check()
    assert sim.mirror == 0 and sim.regs[0x3307] == sim.cached_remote


async def test_an_all_ffff_reply_is_a_failed_read(hass, entry, sim):
    coordinator = entry.runtime_data
    sim.ffff_next = 1
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    await coordinator.async_refresh()
    assert coordinator.last_update_success and state(hass, CLIMATE).state == "cool"


async def test_a_failed_fast_read_does_not_make_the_ac_unavailable(hass, entry, sim):
    await switch(hass, "turn_on")
    sim.ffff_next = 1
    await asyncio.sleep(2.2)
    assert entry.runtime_data.last_update_success and state(hass, CLIMATE).state == "cool"


async def test_an_explained_loss_is_recovered_with_a_warning_only(hass, entry, sim, notifications, monkeypatch, caplog):
    monkeypatch.setattr(ifeel, "VERIFY_GRACE", 0.0)
    coordinator = entry.runtime_data
    await switch(hass, "turn_on")
    coordinator._fast_task.cancel()  # Home Assistant "busy": no reads
    await asyncio.sleep(3.2)
    sim._drop_check()
    assert sim.mirror == 0
    await coordinator.async_refresh()
    await coordinator.async_refresh()  # two polls in a row -> recovery
    await hass.async_block_till_done()
    assert "explained by the reads" in caplog.text
    assert not coordinator.debug.active and notifications == []
    assert state(hass, STATUS).state == "active"
    assert ifeel_writes(sim)[-2:] == [(0x3306, [1, 25]), (0x3307, [25])]


async def test_an_unexplained_loss_logs_stops_and_notifies(hass, entry, sim, notifications, monkeypatch, caplog):
    from custom_components.electra_gemini import debuglog

    monkeypatch.setattr(ifeel, "VERIFY_GRACE", 0.0)
    monkeypatch.setattr(ifeel, "RECOVERY_HOLD", 60.0)
    coordinator = entry.runtime_data
    await switch(hass, "turn_on")
    sim.erase_values, sim.mirror = True, 0  # dropped although the block is read every second
    for _ in range(4):
        await asyncio.sleep(0.3)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert state(hass, STATUS).state == "stopped_value_not_kept" and state(hass, SWITCH).state == STATE_OFF
    assert "NOT explained by the reads" in caplog.text
    assert coordinator.debug.active
    log = Path(coordinator.debug.path)
    assert log.parent.name == "ifeel_logs" and log.name.startswith("ifeel_")
    assert [n for n, _ in notifications] == ["electra_gemini_ifeel_stopped"]
    assert str(log) in notifications[0][1]
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    monkeypatch.setattr(debuglog, "QUIET_AFTER", 0.0)
    await coordinator.async_refresh()
    assert not coordinator.debug.active
    assert [n for n, _ in notifications] == ["electra_gemini_ifeel_stopped", "electra_gemini_ifeel_log"]
    assert '"value_lost_unexplained"' in log.read_text(encoding="utf-8")


# -- settings: the remote turning the AC off / on, Shabbat mode --------------------------------------------------------


async def test_with_the_setting_shabbat_from_home_assistant_pauses_and_resumes(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim, {**OPTIONS, "shabbat_pauses": True})
    await switch(hass, "turn_on")
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.ac_modbus_shabbat_mode"}, blocking=True)
    await entry.runtime_data.async_refresh()
    assert state(hass, STATUS).state == "suspended_shabbat" and state(hass, SWITCH).state == STATE_ON
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    n = len(sim.writes)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ac_modbus_shabbat_mode"}, blocking=True)
    await entry.runtime_data.async_refresh()
    assert state(hass, STATUS).state == "active"
    assert [(w.address, w.values) for w in sim.writes[n:] if w.address == 0x3306][0] == (0x3306, [1, 25])
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_with_the_setting_remote_off_and_on_pauses_and_resumes(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim, {**OPTIONS, "remote_off_on_keeps": True})
    await switch(hass, "turn_on")
    await entry.runtime_data.async_refresh()
    sim.remote_press(mode=0)
    await entry.runtime_data.async_refresh()
    assert state(hass, STATUS).state == "suspended_ac_off" and state(hass, SWITCH).state == STATE_ON
    sim.remote_press(mode=1)
    await entry.runtime_data.async_refresh()
    assert state(hass, STATUS).state == "active" and sim.ifeel_active
    sim.remote_press(mode=0)
    await entry.runtime_data.async_refresh()
    sim.remote_press(mode=2)  # on again in another mode
    await entry.runtime_data.async_refresh()
    assert state(hass, STATUS).state == "stopped_remote_mode" and state(hass, SWITCH).state == STATE_OFF
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_connection_change_hands_back_through_the_old_connection(hass, sim, _defaults):
    bedroom(hass)
    entry = await add_entry(hass, sim)
    await switch(hass, "turn_on")
    n = len(sim.writes)
    hass.config_entries.async_update_entry(entry, data={"host": "localhost", "port": sim.port})
    await hass.async_block_till_done()
    after = [(w.address, w.values) for w in sim.writes[n:]]
    assert after[:2] == [(0x3307, [16]), (0x3306, [0])]  # handed back before the reload
    assert (0x3306, [1, 25]) in after[2:]  # the reloaded integration (new connection) took over
    assert state(hass, SWITCH).state == STATE_ON
    await hass.config_entries.async_unload(entry.entry_id)


async def test_dry_from_home_assistant_suspends_and_cool_resumes(hass, entry, sim):
    await switch(hass, "turn_on")
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "dry"}, blocking=True)
    assert state(hass, STATUS).state == "suspended_dry" and state(hass, SWITCH).state == STATE_ON
    assert ifeel_writes(sim)[-2:] == [(0x3307, [16]), (0x3306, [0])]
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "cool"}, blocking=True)
    assert state(hass, STATUS).state in ("active", "holding_start")
    assert ifeel_writes(sim)[-2][0] == 0x3306 and len(ifeel_writes(sim)[-2][1]) == 2


async def test_heat_from_home_assistant_keeps_the_internal_cells_usable(hass, entry, sim):
    """0x4801 shows Heat as 0x0100 (checked with the mask 0x0F00): the cells stay usable in Heat."""
    await switch(hass, "turn_on")
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "heat"}, blocking=True)
    for _ in range(3):
        await entry.runtime_data.async_refresh()
    assert entry.runtime_data.data.internal is not None
    assert state(hass, STATUS).state in ("active", "holding_start")
    assert state(hass, "sensor.ac_modbus_compressor_state").state != "unavailable"


async def test_ifeel_control_is_refused_in_auto(hass, entry, sim):
    from homeassistant.exceptions import HomeAssistantError

    sim.set_registers(mode=3)
    await entry.runtime_data.async_refresh()
    with pytest.raises(HomeAssistantError, match="Auto"):
        await switch(hass, "turn_on")
    assert state(hass, STATUS).state == "refused_auto" and state(hass, SWITCH).state == STATE_OFF
    assert ifeel_writes(sim) == []
