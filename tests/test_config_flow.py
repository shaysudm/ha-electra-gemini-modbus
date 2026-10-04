# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config flow and options flow."""
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electra_gemini import config_flow, modbus
from custom_components.electra_gemini.const import DOMAIN


@pytest.fixture(autouse=True)
def fast_client(monkeypatch):
    original = modbus.ModbusTcpClient.__init__

    def init(self, *a, **k):
        k.update(timeout=0.3, reconnect_wait=0.1, connect_timeout=0.5)
        original(self, *a, **k)

    monkeypatch.setattr(modbus.ModbusTcpClient, "__init__", init)
    monkeypatch.setattr(config_flow, "VALIDATE_RETRY_DELAY", 0.0)


@pytest.fixture(autouse=True)
def no_setup(monkeypatch):
    async def fake_setup(hass, entry):
        return True

    monkeypatch.setattr("custom_components.electra_gemini.async_setup_entry", fake_setup)


async def test_form_has_no_prefilled_host_and_creates_entry(hass, sim):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    keys = {str(k): k for k in schema}
    from voluptuous.schema_builder import UNDEFINED

    assert keys["host"].default is UNDEFINED  # no IP prefilled
    assert keys["port"].default() == 8899
    assert "unit_id" not in keys  # fixed at 160, not configurable
    assert keys["name"].default() == "AC"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": sim.port, "name": "AC"}
    )
    # the internal cells are usable: a second screen for the IFeel room sensors
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "sensors"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ifeel_sensors": ["sensor.bedroom"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AC"
    assert result["data"] == {"host": "127.0.0.1", "port": sim.port}
    assert result["options"] == {"ifeel_sensors": ["sensor.bedroom"]}
    assert sim.writes == []


async def test_the_sensors_screen_can_be_left_empty(hass, sim):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": sim.port, "name": "AC"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["options"] == {"ifeel_sensors": []}


@pytest.mark.parametrize("fault", ["silent", "error", "layout", "ffff"])
async def test_unusable_internal_cells_give_a_warning_then_create_the_entry(hass, sim, monkeypatch, fault):
    if fault == "silent":
        sim.internal_silent = True
    elif fault == "error":
        sim.internal_error = 2
    elif fault == "layout":
        sim.internal_cells = {0x480B: 0x1400 | 99}  # room 99 in the cell, not the board's room temperature
    else:
        original = sim.read
        monkeypatch.setattr(sim, "read", lambda u, a, c: [0xFFFF] * c if u == 1 else original(u, a, c))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": sim.port, "name": "AC"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "no_slave1"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "127.0.0.1", "port": sim.port} and result["options"] == {}
    assert sim.writes == []


async def test_one_all_ffff_status_reply_is_retried(hass, sim):
    sim.ffff_next = 1
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": sim.port, "name": "AC"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "sensors"


async def test_cannot_connect(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": 1, "name": "AC"}
    )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "cannot_connect"}


async def test_device_that_does_not_answer_as_unit_160_is_reported_as_cannot_connect(hass, sim):
    sim.unit_control = 5  # something answers on the port, but not like our unit
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": sim.port, "name": "AC"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_is_aborted(hass, sim):
    MockConfigEntry(
        domain=DOMAIN, unique_id=f"127.0.0.1:{sim.port}:160", data={"host": "127.0.0.1", "port": sim.port}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": sim.port, "name": "AC"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_flow(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "h", "port": 8899})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 20,
            "ifeel": {
                "ifeel_sensors": ["sensor.bedroom"],
                "cold_tolerance": 0.3,
                "hot_tolerance": 0.7,
                "keepalive": 120,
                "min_off": "timer",
                "stale_minutes": 30,
                "coil_limit": 5,
                "max_run": 45,
                "remote_off_on_keeps": True,
                "shabbat_pauses": True,
            },
            "unsafe": {"allow_without_coil_protection": False, "override_max_run": 10},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["scan_interval"] == 20 and data["ifeel_sensors"] == ["sensor.bedroom"]
    assert data["hot_tolerance"] == 0.7 and data["keepalive"] == 120 and data["allow_without_coil_protection"] is False
    assert data["remote_off_on_keeps"] is True and data["shabbat_pauses"] is True
    assert data["heat_min_off"] == "5" and data["heat_coil_limit"] == 65  # the Heat defaults (2026-10-03)


async def test_options_flow_refuses_values_out_of_range(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "h", "port": 8899})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    ifeel = {"cold_tolerance": 0.5, "hot_tolerance": 0.5, "keepalive": 180, "min_off": "timer", "stale_minutes": 30,
             "coil_limit": 2, "max_run": 45}  # coil limit below the 3 °C floor
    with pytest.raises(Exception):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_interval": 10, "ifeel": ifeel, "unsafe": {"allow_without_coil_protection": False, "override_max_run": 10}}
        )


# -- reconfigure: the connection ---------------------------------------------------------------------------------


async def _reconfigure(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


async def test_reconfigure_shows_the_connection(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "10.1.2.3", "port": 8899})
    entry.add_to_hass(hass)
    result = await _reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    keys = {str(k): k for k in result["data_schema"].schema}
    assert set(keys) == {"host", "port"}
    assert keys["host"].description["suggested_value"] == "10.1.2.3" and keys["port"].description["suggested_value"] == 8899


async def test_reconfigure_changes_the_connection_after_checking_it(hass, sim):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="10.9.9.9:8899:160", data={"host": "10.9.9.9", "port": 8899}, options={"scan_interval": 10}
    )
    entry.add_to_hass(hass)
    result = await _reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": 1})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "cannot_connect"}
    keys = {str(k): k for k in result["data_schema"].schema}
    assert keys["port"].description["suggested_value"] == 1  # what was typed is kept
    assert entry.data == {"host": "10.9.9.9", "port": 8899}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": " 127.0.0.1 ", "port": sim.port})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data == {"host": "127.0.0.1", "port": sim.port}
    assert entry.unique_id == f"127.0.0.1:{sim.port}:160"
    assert entry.options == {"scan_interval": 10}
    assert sim.writes == []


async def test_reconfigure_refuses_a_connection_used_by_another_entry(hass, sim):
    MockConfigEntry(domain=DOMAIN, unique_id=f"127.0.0.1:{sim.port}:160", data={"host": "127.0.0.1", "port": sim.port}).add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="10.9.9.9:8899:160", data={"host": "10.9.9.9", "port": 8899})
    entry.add_to_hass(hass)
    result = await _reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": sim.port})
    assert result["errors"] == {"base": "already_configured"} and entry.data["host"] == "10.9.9.9"


async def test_options_have_no_connection_fields(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "h", "port": 8899})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k) for k in result["data_schema"].schema}
    assert keys == {"scan_interval", "ifeel", "unsafe"}
