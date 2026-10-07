# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data update coordinator: polling, the (coalesced, rate-limited) writes, and IFeel control."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import Context, Event as HassEvent, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import debuglog
from .const import (
    CONF_ALLOW_NO_COIL,
    CONF_COIL_LIMIT,
    CONF_COLD_TOLERANCE,
    CONF_HEAT_COIL_LIMIT,
    CONF_HEAT_MIN_OFF,
    CONF_HOT_TOLERANCE,
    CONF_IFEEL_SENSORS,
    CONF_KEEPALIVE,
    CONF_MAX_RUN,
    CONF_MIN_OFF,
    CONF_OVERRIDE_MAX_RUN,
    CONF_REMOTE_OFF_ON_KEEPS,
    CONF_SHABBAT_PAUSES,
    CONF_STALE_MINUTES,
    DOMAIN,
    IFEEL_DEFAULTS,
    INTERNAL_MAX_FAILURES,
    INTERNAL_RETRY_INTERVAL,
    INTERNAL_UNIT_ID,
    MIN_OFF_TIMER,
    MIN_WRITE_INTERVAL,
    READBACK_DELAYS,
)
from .ifeel import (
    FAST_READ_INTERVAL,
    STOPPED_SENSOR,
    STOPPED_IFEEL_UNSUPPORTED,
    STOPPED_VALUE_NOT_KEPT,
    Event,
    IFeelConfig,
    IFeelController,
    IFeelRefused,
    Observation,
    SensorReading,
    Write,
)
from .modbus import MAX_READ_COUNT, ModbusError, ModbusTcpClient, ModbusTimeout
from .registers import (
    IDENTITY_ADDRESS,
    IDENTITY_COUNT,
    SERIAL_ADDRESSES,
    SNAPSHOT_RANGES,
    INTERNAL_ADDRESS,
    INTERNAL_COUNT,
    MARKER_ADDRESS,
    MARKER_COUNT,
    MODE_AUTO,
    MODE_COOL,
    MODE_OFF,
    REG_FAN,
    REG_MODE,
    REG_SETPOINT,
    STATUS_ADDRESS,
    STATUS_COUNT,
    Identity,
    Internal,
    Overrides,
    Status,
    WriteRequest,
    coil_plausible,
    marker_seen,
    parse_identity,
    parse_internal,
    parse_status,
    plan_write,
    with_overrides,
    writes_applied,
)
from .writer import WriteCoalescer

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
NOTIFY_STOPPED = "ifeel_stopped"
NOTIFY_LOG = "ifeel_log"
# A value lost although the status block was read at least this often is not explained by the read rate (debug log).
UNEXPLAINED_GAP = 2.0


@dataclass(frozen=True)
class AcData:
    status: Status  # what the user should see (Standby overrides applied)
    internal: Internal | None  # None if the unit-1 read failed: only the entities based on it become unavailable
    raw_status: Status | None = None  # the unit's own values


def ifeel_options(options: dict[str, Any]) -> dict[str, Any]:
    return {key: options.get(key, default) for key, default in IFEEL_DEFAULTS.items()}


def ifeel_config(options: dict[str, Any]) -> IFeelConfig:
    o = ifeel_options(options)
    min_off = o[CONF_MIN_OFF]
    return IFeelConfig(
        cold_tolerance=float(o[CONF_COLD_TOLERANCE]),
        hot_tolerance=float(o[CONF_HOT_TOLERANCE]),
        keepalive=float(o[CONF_KEEPALIVE]),
        min_off_minutes=None if min_off in (None, MIN_OFF_TIMER) else int(min_off),
        heat_min_off_minutes=int(o[CONF_HEAT_MIN_OFF]),
        coil_limit=int(o[CONF_COIL_LIMIT]),
        heat_coil_limit=int(o[CONF_HEAT_COIL_LIMIT]),
        stale_minutes=int(o[CONF_STALE_MINUTES]),
        allow_without_coil_protection=bool(o[CONF_ALLOW_NO_COIL]),
        override_max_run_minutes=int(o[CONF_OVERRIDE_MAX_RUN]),
        max_run_minutes=int(o[CONF_MAX_RUN]),
        remote_off_on_keeps=bool(o[CONF_REMOTE_OFF_ON_KEEPS]),
        shabbat_pauses=bool(o[CONF_SHABBAT_PAUSES]),
    )


class ElectraCoordinator(DataUpdateCoordinator[AcData]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ModbusTcpClient,
        unit_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._scan_interval = scan_interval
        self._client = client
        self._unit_id = unit_id
        self._overrides = Overrides()
        self._internal: Internal | None = None
        self._internal_failures = 0
        self._internal_retry_at = 0.0
        self._coil_implausible = False
        self.last_active_mode = MODE_COOL
        self.identity: Identity | None = None  # read once at setup (device info)
        self._last_fast_regs: list[int] | None = None  # the last fast read recorded in the history
        self._in_auto = False  # for the log line when the unit is seen entering Auto
        self._writer = WriteCoalescer(self._apply_write, MIN_WRITE_INTERVAL)
        # IFeel control
        self.ifeel = IFeelController(ifeel_config(entry.options), time.monotonic)
        self.ifeel_sensors: list[str] = list(ifeel_options(entry.options)[CONF_IFEEL_SENSORS])
        self.room_sensor: str | None = None
        self._unsub_sensor = None
        self._ifeel_lock = asyncio.Lock()
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.keep_ifeel_on_unload = False  # set for a reload caused by an options change (rule 10)
        self.connection: tuple[str, int] | None = None  # (host, port) this coordinator was set up with
        self.debug = debuglog.DebugLog(Path(hass.config.path(DOMAIN, "ifeel_logs")))
        self._raw: dict[str, Any] = {}
        self._fast_task: asyncio.Task | None = None
        self._read_times: deque[float] = deque(maxlen=400)  # successful status-block reads (monotonic)
        self._last_read_result = "none"
        self._check_now = False
        client.on_event = self._client_event

    # -- setup / teardown -----------------------------------------------------------------------------------------

    async def async_setup_ifeel(self) -> None:
        """After the first poll: restore the chosen room sensor and IFeel control (decided: restored after a restart)."""
        stored = await self._store.async_load() or {}
        self.debug.record({"event": "integration_start", "stored": stored})
        sensor = stored.get("sensor")
        if sensor not in self.ifeel_sensors:
            sensor = self.ifeel_sensors[0] if len(self.ifeel_sensors) == 1 else None
        self._set_room_sensor(sensor)
        self.ifeel.set_sensor(self._reading())  # IFeel control is not on yet: no writes
        if stored.get("enabled"):
            try:
                await self._run_actions(self.ifeel.request_enable())
            except IFeelRefused as err:
                # IFeel may still be on in the unit (an options reload does not hand back): hand back now
                _LOGGER.warning("IFeel control not restored: %s", err)
                status = err.status or STOPPED_SENSOR
                await self._run_actions(self.ifeel.stop(status, str(err)))
                await self._save()
        self.async_update_listeners()

    async def async_handback(self) -> None:
        """Unload or Home Assistant stop: hand back to the unit's own sensor (IFeel control stays switched on)."""
        if self.keep_ifeel_on_unload:
            self.debug.record({"event": "reload_keep_ifeel"})
            return
        self.debug.record({"event": "handback_on_unload"})
        try:
            await self._run_actions(self.ifeel.handback_for_unload())
        except Exception as err:  # noqa: BLE001 - never block the unload
            _LOGGER.warning("Could not hand IFeel back on unload: %s", err)

    async def async_shutdown(self) -> None:
        if self._fast_task is not None:
            self._fast_task.cancel()
        if self._unsub_sensor:
            self._unsub_sensor()
            self._unsub_sensor = None
        await self._flush_log()
        await super().async_shutdown()
        await self._writer.shutdown()
        await self._client.close()

    # -- polling --------------------------------------------------------------------------------------------------

    async def _read_status(self) -> Status:
        """The status block 0x3300 x 19. Every successful read is timed: the unit keeps an IFeel value only while it
        is read at least about every 2 s. An all-0xFFFF reply (seen with two TCP clients) is a failed read."""
        try:
            regs = await self._client.read_holding_registers(self._unit_id, STATUS_ADDRESS, STATUS_COUNT)
        except ModbusTimeout:
            self._last_read_result = "timeout"
            raise
        except ModbusError as err:
            self._last_read_result = f"error ({err})"
            raise
        if all(value == 0xFFFF for value in regs):
            self._last_read_result = "all 0xFFFF"
            raise ValueError("all 0xFFFF")
        self._last_read_result = "ok"
        self._read_times.append(time.monotonic())
        self._raw["s160"] = regs
        return parse_status(regs)

    async def async_read_identity(self) -> None:
        """The controller board's and the indoor unit's identifiers (slave 1, static), for the device info. Read once at
        setup when the internal cells are usable; a failure only leaves them out."""
        if self.data is None or self.data.internal is None:
            return
        half = IDENTITY_COUNT // 2
        try:
            regs = await self._client.read_holding_registers(INTERNAL_UNIT_ID, IDENTITY_ADDRESS, half)
            regs += await self._client.read_holding_registers(INTERNAL_UNIT_ID, IDENTITY_ADDRESS + half, half)
            if all(value == 0xFFFF for value in regs):
                raise ValueError("all 0xFFFF (a failed read)")
            self.identity = parse_identity(regs)
        except (ModbusError, ValueError) as err:
            _LOGGER.debug("Identity block not read: %s", err)

    async def async_slave1_snapshot(self) -> dict[str, Any]:
        """For diagnostics: read every unit-1 address that answered in the full scan (read only), the serial-number
        characters masked (null). Two failed reads in a row skip the rest of a range (a board without unit 1, or with
        a different layout, must not hold the download up)."""
        started = time.monotonic()
        blocks: list[dict[str, Any]] = []
        for first, last in SNAPSHOT_RANGES:
            failures = 0
            address = first
            while address <= last:
                count = min(MAX_READ_COUNT, last - address + 1)
                try:
                    values = await self._client.read_holding_registers(INTERNAL_UNIT_ID, address, count)
                except ModbusError as err:
                    blocks.append({"address": f"0x{address:04X}", "count": count, "error": str(err)})
                    failures += 1
                    if failures >= 2 and address + count <= last:
                        blocks.append({
                            "address": f"0x{address + count:04X}", "count": last - address - count + 1,
                            "error": "skipped after two failed reads in a row",
                        })  # fmt: skip
                        break
                else:
                    failures = 0
                    blocks.append({
                        "address": f"0x{address:04X}",
                        "values": [None if address + i in SERIAL_ADDRESSES else v for i, v in enumerate(values)],
                    })  # fmt: skip
                address += count
        return {
            "ranges": [f"0x{first:04X}-0x{last:04X}" for first, last in SNAPSHOT_RANGES],
            "masked": "serial-number characters (0x4040-0x4045, 0x4052-0x4056, 0x4813-0x481A) are null",
            "duration_s": round(time.monotonic() - started, 1),
            "blocks": blocks,
        }

    # -- the history: who asked for what --------------------------------------------------------------------------

    def record_request(self, entity_id: str | None, action: str, details: dict[str, Any], context: Context | None) -> None:
        """A request from an entity (a service call), with where it came from."""
        self.debug.record({"request": action, "entity": entity_id, **details, "origin": self._origin(context)})

    def record_refusal(self, entity_id: str | None, action: str, reason: str) -> None:
        self.debug.record({"refused": action, "entity": entity_id, "reason": reason})

    def _origin(self, context: Context | None) -> dict[str, Any]:
        """Who caused a request: an automation or a script (by entity), a user (no name: diagnostics may be shared),
        or Home Assistant itself."""
        if context is None:
            return {"by": "unknown"}
        for state in self.hass.states.async_all(("automation", "script")):
            if state.context.id in (context.id, context.parent_id):
                return {"by": state.domain, "entity": state.entity_id}
        if context.user_id:
            return {"by": "user"}
        if context.parent_id:
            return {"by": "other", "note": "caused by another event"}
        return {"by": "home_assistant"}

    def _slave1_usable(self) -> bool:
        return self._internal_failures < INTERNAL_MAX_FAILURES

    def _build(self, status: Status) -> AcData:
        if status.mode not in (None, MODE_OFF, MODE_AUTO):
            self.last_active_mode = status.mode  # never Auto: "turn on" must not write it (owner decision 2026-10-04)
        in_auto = status.mode == MODE_AUTO
        if in_auto and not self._in_auto:
            _LOGGER.warning(
                "The AC was put in Auto mode (at the remote). Auto is not supported: on this unit it can freeze the "
                "indoor coil. Setpoint and fan changes from Home Assistant are refused until another mode is chosen"
            )
        self._in_auto = in_auto
        if status.mode != MODE_OFF and self._overrides != Overrides():
            self._overrides = Overrides()  # the unit was turned on some other way, its own values are current
        return AcData(with_overrides(status, self._overrides), self._internal, status)

    async def _async_update_data(self) -> AcData:
        started = time.monotonic()
        self._raw = {}
        try:
            status = await self._read_status()
        except (ModbusError, ValueError) as err:
            self.debug.record({"phase": "poll", "error": str(err)})
            raise UpdateFailed(f"Cannot read the AC: {err}") from err
        self._internal = await self._read_internal(status)
        if self.debug.active:
            await self._read_extra()
        self.debug.record({"phase": "poll", **self._raw, "read_ms": round((time.monotonic() - started) * 1000)})
        obs = Observation(status, self._internal, self._slave1_usable(), True)
        await self._run_actions(self.ifeel.update(obs))
        self._check_log()  # also when IFeel control had nothing to write
        await self._flush_log()
        self._ensure_fast_reads()
        return self._build(status)

    # -- fast status reads while IFeel is on ----------------------------------------------------------------------

    def _ensure_fast_reads(self) -> None:
        if self.ifeel.needs_fast_reads and (self._fast_task is None or self._fast_task.done()):
            self._fast_task = self.config_entry.async_create_background_task(
                self.hass, self._fast_reads(), "electra_gemini IFeel status reads"
            )

    async def _fast_reads(self) -> None:
        """While IFeel is on: the status block every second (decided 2026-10-03). The reads also update the entities
        and the controller; a failed one does not make anything unavailable (the normal polls decide that)."""
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while self.ifeel.needs_fast_reads:
            next_at += FAST_READ_INTERVAL
            try:
                status = await self._read_status()
            except (ModbusError, ValueError) as err:
                _LOGGER.debug("Fast status read failed: %s", err)
                self.debug.record({"phase": "fast", "error": str(err)})
            else:
                regs = self._raw.get("s160")
                if regs != self._last_fast_regs:  # only the fast reads where something changed
                    self._last_fast_regs = regs
                    self.debug.record({"phase": "fast", "s160": regs})
                actions = self.ifeel.update(Observation(status, self._internal, self._slave1_usable(), fresh=False))
                if self.data is not None:
                    self.data = self._build(status)  # without rescheduling the normal polls
                if actions:
                    # not awaited: the reads must go on while an enable sequence waits its 1.5 s
                    self.hass.async_create_task(self._run_actions(actions))
                else:
                    self.async_update_listeners()
            delay = next_at - loop.time()
            if delay < 0:
                next_at, delay = loop.time(), 0
            await asyncio.sleep(delay)

    async def _do_check_now(self) -> None:
        """Fast detection: 0x3307 shows a lost value; read the slave-1 cells now and let them decide."""
        try:
            status = await self._read_status()
        except (ModbusError, ValueError):
            return  # the next poll checks
        self._internal = await self._read_internal(status)
        await self._run_actions(self.ifeel.update(Observation(status, self._internal, self._slave1_usable(), True)))

    def _read_gap_since(self, since: float) -> float:
        """The longest gap between successful status reads from `since` until now (seconds)."""
        points = [since] + [t for t in self._read_times if t > since] + [time.monotonic()]
        return max(b - a for a, b in zip(points, points[1:]))

    async def _read_internal(self, status: Status) -> Internal | None:
        """The optional unit-1 cells, or None if they cannot be read or do not look as expected (then only the
        entities based on them are unavailable). Never raises."""
        now = self.hass.loop.time()
        if now < self._internal_retry_at:
            return None
        try:
            regs = await self._client.read_holding_registers(INTERNAL_UNIT_ID, INTERNAL_ADDRESS, INTERNAL_COUNT)
            self._raw["blk4801"] = regs
            if all(value == 0xFFFF for value in regs):
                raise ValueError("all 0xFFFF (a failed read)")
            internal = parse_internal(regs, status.room_temp, status.mode)
            if self.ifeel.enabled or self.debug.active:
                marker = await self._client.read_holding_registers(INTERNAL_UNIT_ID, MARKER_ADDRESS, MARKER_COUNT)
                self._raw["mk4850"] = marker
                internal = replace(internal, marker=marker_seen(marker))
        except (ModbusError, ValueError) as err:
            self._internal_failures += 1
            if self._internal_failures == 1:
                _LOGGER.warning(
                    "Internal status cells (unit %s) not usable, the compressor and coil entities are unavailable: %s",
                    INTERNAL_UNIT_ID,
                    err,
                )
            else:
                _LOGGER.debug("Internal status cells still not usable: %s", err)
            if self._internal_failures >= INTERNAL_MAX_FAILURES:
                self._internal_retry_at = now + INTERNAL_RETRY_INTERVAL
            return None
        if self._internal_failures:
            _LOGGER.info("Internal status cells (unit %s) usable again", INTERNAL_UNIT_ID)
        self._internal_failures = 0
        self._internal_retry_at = 0.0
        if internal.coil_temp is not None and not coil_plausible(internal.coil_temp, status, internal.running):
            if not self._coil_implausible:
                _LOGGER.warning(
                    "Coil temperature %s °C is not plausible (mode %s, room %s °C, compressor running %s); "
                    "the coil temperature entity is unavailable until it is",
                    internal.coil_temp,
                    status.mode,
                    status.room_temp,
                    internal.running,
                )
            self._coil_implausible = True
            return replace(internal, coil_temp=None)
        self._coil_implausible = False
        return internal

    async def _read_extra(self) -> None:
        """While the debug log records: the stored settings words 0x4005 / 0x4025 (reads only)."""
        for address, key in ((0x4005, "s4005"), (0x4025, "s4025")):
            try:
                self._raw[key] = (await self._client.read_holding_registers(INTERNAL_UNIT_ID, address, 1))[0]
            except ModbusError as err:
                self._raw[key] = f"error: {err}"

    # -- control --------------------------------------------------------------------------------------------------

    async def async_set(
        self,
        *,
        mode: int | None = None,
        fan: int | None = None,
        setpoint: int | None = None,
        shabat: bool | None = None,
        sleep: bool | None = None,
    ) -> None:
        """Request a change. Merged with other requests made close by and sent at most every MIN_WRITE_INTERVAL."""
        request = WriteRequest(mode=mode, fan=fan, setpoint=setpoint, shabat=shabat, sleep=sleep)
        try:
            await self._writer.submit(request)
        except (ModbusError, ValueError) as err:
            asked = {k: v for k, v in asdict(request).items() if v is not None}
            self.debug.record({"request_failed": str(err), "request": asked})
            raise HomeAssistantError(f"Cannot control the AC: {err}") from err

    async def _apply_write(self, request: WriteRequest) -> None:
        status = await self._read_status()  # fresh state, not the up to 10 s old poll
        plan = plan_write(status, self._overrides, request)
        expected = _expected_settings(status, plan.writes)
        if expected != (status.mode, status.fan, status.setpoint):
            self.ifeel.expect_own_settings(*expected)  # IFeel control must not take its own write for the remote
        for address, values in plan.writes:
            await self._write(address, values, "settings")
        self._overrides = plan.overrides
        if not plan.writes:  # nothing was sent (a change while in Standby, or no change): just show it
            self.async_set_updated_data(self._build(await self._read_status()))
            return
        # The unit shows a write a moment later, not at once: read back until it shows what was written.
        status = None
        for delay in READBACK_DELAYS:
            await asyncio.sleep(delay)
            try:
                status = await self._read_status()
            except (ModbusError, ValueError) as err:
                _LOGGER.debug("Readback after write failed: %s", err)
                await self.async_request_refresh()  # the write itself went through
                return
            if writes_applied(status, plan.writes):
                break
        else:
            _LOGGER.warning("The AC did not show %s after %.1f s", plan.writes, sum(READBACK_DELAYS))
        obs = Observation(status, self._internal, self._slave1_usable(), fresh=False)
        await self._run_actions(self.ifeel.update(obs))
        self.async_set_updated_data(self._build(status))

    async def _write(self, address: int, values: list[int] | tuple[int, ...], why: str) -> None:
        values = list(values)
        started = time.monotonic()
        kind = "FC6" if len(values) == 1 else "FC16"
        try:
            if len(values) == 1:
                await self._client.write_register(self._unit_id, address, values[0])
            else:
                await self._client.write_registers(self._unit_id, address, values)
        except (ModbusError, ValueError) as err:
            self.debug.record({"write": f"{kind} 0x{address:04X} = {values}", "why": why, "result": f"error: {err}"})
            raise
        self.debug.record(
            {
                "write": f"{kind} 0x{address:04X} = {values}",
                "why": why,
                "result": "ok",
                "ms": round((time.monotonic() - started) * 1000),
            }
        )

    # -- IFeel control --------------------------------------------------------------------------------------------

    async def async_ifeel_enable(self) -> None:
        try:
            await self._run_actions(self.ifeel.request_enable())
        except IFeelRefused as err:
            self.record_refusal(None, "ifeel_control_on", f"{err} ({err.status})")
            self.async_update_listeners()
            raise HomeAssistantError(f"IFeel control cannot be switched on: {err}") from err
        finally:
            await self._save()

    async def async_ifeel_disable(self) -> None:
        await self._run_actions(self.ifeel.request_disable())
        await self._save()

    async def async_set_room_sensor(self, entity_id: str | None) -> None:
        self._set_room_sensor(entity_id)
        self.debug.record({"event": "room_sensor", "entity_id": entity_id})
        await self._run_actions(self.ifeel.set_sensor(self._reading()))
        await self._save()
        self.async_update_listeners()

    def update_ifeel_sensors(self, sensors: list[str]) -> None:
        self.ifeel_sensors = list(sensors)

    def _set_room_sensor(self, entity_id: str | None) -> None:
        if self._unsub_sensor:
            self._unsub_sensor()
            self._unsub_sensor = None
        self.room_sensor = entity_id
        if entity_id:
            self._unsub_sensor = async_track_state_change_event(self.hass, [entity_id], self._sensor_changed)

    @callback
    def _sensor_changed(self, event: HassEvent[EventStateChangedData]) -> None:
        reading = self._reading()
        self.debug.record({"sensor": reading.entity_id, "value": reading.value})
        actions = self.ifeel.set_sensor(reading)
        if actions:
            self.hass.async_create_task(self._run_actions(actions))
        else:
            self.async_update_listeners()

    def _reading(self) -> SensorReading:
        if self.room_sensor is None:
            return SensorReading(None, None, None)
        state = self.hass.states.get(self.room_sensor)
        return SensorReading(self.room_sensor, _celsius(state), _reported_at(state))

    async def _run_actions(self, actions: list[Write | Event]) -> None:
        """Carry out the controller's writes in order (one batch at a time) and handle its events."""
        if not actions:
            return
        async with self._ifeel_lock:
            for action in actions:
                if isinstance(action, Event):
                    self._ifeel_event(action)
                    continue
                if action.delay:
                    await asyncio.sleep(action.delay)
                try:
                    await self._write(action.address, action.values, action.reason)
                except (ModbusError, ValueError) as err:
                    _LOGGER.warning("IFeel write %s failed: %s", action.reason, err)
                    self.ifeel.write_failed(action)
        if self._check_now:
            self._check_now = False
            self.hass.async_create_task(self._do_check_now())
        self._check_log()
        await self._flush_log()
        self._ensure_fast_reads()
        self.async_update_listeners()

    def _ifeel_event(self, event: Event) -> None:
        # with the last raw reads, so that a report shows what the decision was based on
        self.debug.event(event.kind, {**event.data, "raw": dict(self._raw)})
        if event.kind == "check_now":
            self._check_now = True
        elif event.kind == "value_lost":
            self._value_lost(event.data)
        elif event.kind == "stopped":
            _LOGGER.info("IFeel control stopped: %s %s", event.data.get("status"), event.data.get("detail") or "")
            self.hass.async_create_task(self._save())
            if event.data.get("status") == STOPPED_VALUE_NOT_KEPT:
                log = f"\n\nA log of what happened is being saved to:\n`{self.debug.path}`" if self.debug.active else ""
                persistent_notification.async_create(
                    self.hass,
                    "IFeel control was switched off because the air conditioner did not keep the temperature values "
                    "sent by Home Assistant. The air conditioner is using its own temperature sensor. You can switch "
                    "IFeel control on again." + log,
                    title="AC: IFeel control stopped",
                    notification_id=f"{DOMAIN}_{NOTIFY_STOPPED}",
                )
            elif event.data.get("status") == STOPPED_IFEEL_UNSUPPORTED:
                persistent_notification.async_create(
                    self.hass,
                    "IFeel control was switched off: the air conditioner switched IFeel on, but kept using another "
                    "temperature (the remote's) instead of the one sent by Home Assistant. This controller board may "
                    "not support IFeel over Modbus (this was seen on board 1A0040). The rest of the integration works "
                    "as usual. See \"Compatibility\" in the integration's README.",
                    title="AC: IFeel control not supported",
                    notification_id=f"{DOMAIN}_{NOTIFY_STOPPED}",
                )
        elif event.kind == "warning":
            _LOGGER.warning("IFeel: %s", event.data.get("message"))

    def _value_lost(self, data: dict) -> None:
        """One warning per lost value; the debug log only when the reads were no more than 2 s apart."""
        gap = self._read_gap_since(data.get("written_at", time.monotonic()))
        explained = gap > UNEXPLAINED_GAP
        _LOGGER.warning(
            "IFeel value %s lost (the AC used %s) %.1f s after it was written; longest gap between status reads since "
            "then %.1f s, last read: %s; %s",
            data.get("written"),
            data.get("in_use"),
            data.get("since_write", 0),
            gap,
            self._last_read_result,
            "explained by the reads" if explained else "NOT explained by the reads: debug log",
        )
        if explained:
            return
        first = not self.debug.active
        self.debug.event("value_lost_unexplained", {**data, "max_read_gap": round(gap, 2)})
        if first:
            _LOGGER.warning("IFeel debug log started: %s", self.debug.path)
            self.hass.async_add_executor_job(debuglog.prune, self.debug.directory, debuglog.KEEP_FILES - 1)

    def _check_log(self) -> None:
        reason = self.debug.due_close()
        if reason is None:
            return
        summary = self.debug.close(reason)
        _LOGGER.info(
            "IFeel debug log closed (%s): %s unexplained losses, %s", reason, summary.unexplained_losses, summary.path
        )
        persistent_notification.async_create(
            self.hass,
            "IFeel control lost a temperature value although Home Assistant was reading the air conditioner often "
            f"enough. A log of what happened was saved to:\n`{summary.path}`\n\nPlease help find the cause by "
            "sharing this file in an issue on GitHub: " + _issue_tracker(self.hass),
            title="AC: IFeel control",
            notification_id=f"{DOMAIN}_{NOTIFY_LOG}",
        )

    async def _flush_log(self) -> None:
        path, lines = self.debug.take_pending()
        if path is not None and lines:
            await self.hass.async_add_executor_job(debuglog.write_lines, path, lines)

    @callback
    def _client_event(self, kind: str, detail: str) -> None:
        self.debug.record({"conn": kind, "detail": detail})

    async def _save(self) -> None:
        await self._store.async_save({"enabled": self.ifeel.enabled, "sensor": self.room_sensor})


def _expected_settings(status: Status, writes: list[tuple[int, list[int]]]) -> tuple:
    mode, fan, setpoint = status.mode, status.fan, status.setpoint
    for address, values in writes:
        for i, value in enumerate(values):
            reg = address + i
            if reg == REG_MODE:
                mode = value
            elif reg == REG_FAN:
                fan = value
            elif reg == REG_SETPOINT:
                setpoint = value
    return mode, fan, setpoint


def _celsius(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature.CELSIUS)
    if unit != UnitOfTemperature.CELSIUS:
        try:
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        except HomeAssistantError:
            return None
    return value


def _reported_at(state: State | None) -> float | None:
    """When the sensor last reported, on the controller's (monotonic) clock."""
    if state is None:
        return None
    reported = getattr(state, "last_reported", None) or state.last_updated
    age = (dt_util.utcnow() - reported).total_seconds()
    return time.monotonic() - max(0.0, age)


def _issue_tracker(hass: HomeAssistant) -> str:
    try:
        from homeassistant.loader import async_get_loaded_integration

        return async_get_loaded_integration(hass, DOMAIN).issue_tracker or ""
    except Exception:  # noqa: BLE001
        return ""
