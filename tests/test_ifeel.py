# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The IFeel controller against the unit model on a virtual clock (no network, no Home Assistant).

Every scenario also checks, through the harness, that every write is on the client's allow-list and that no start
value is ever sent while the 8-minute timer bit is set with the compressor idle (unless a custom off time is set).
"""
import pytest

from custom_components.electra_gemini import ifeel
from custom_components.electra_gemini.ifeel import IFeelConfig, IFeelRefused
from ifeel_harness import Harness


def clean(h: Harness) -> None:
    assert h.violations == []


def writes_after(h: Harness, t0: float):
    return [(t, w.address, w.values) for t, w in h.writes if t >= t0]


# -- enable, thermostat, rule 1 / 2 / 3 ------------------------------------------------------------------------------


def test_enable_uses_one_block_then_repeats_the_value():
    h = Harness(room=25.0)
    h.enable()
    (t1, a1, v1), (t2, a2, v2) = writes_after(h, 0)
    assert (a1, v1) == (0x3306, (1, 25)) and (a2, v2) == (0x3307, (25,))
    assert t2 - t1 == pytest.approx(1.5)
    h.run(10)
    assert h.unit.running and h.ctrl.status == ifeel.ACTIVE
    clean(h)


def test_starting_state_between_the_switching_points_is_off():
    h = Harness(room=24.3)  # between 23.5 and 24.5
    h.enable()
    assert h.values_written() == [23, 23]
    assert h.ctrl.command == "off"


def test_cool_cycles_with_on_and_off_values_only_and_keepalives():
    h = Harness(room=24.6)
    h.enable()
    h.run(3 * 3600)
    assert set(h.values_written()) <= {23, 25}
    assert len(h.unit.starts) >= 3
    assert h.unit.active and h.unit.source == 0x2000  # never expired
    keepalives = [t for t, w in h.writes if w.values == (1,)]
    assert keepalives and all(b - a <= 190 for a, b in zip(keepalives, keepalives[1:]))
    clean(h)


def test_new_values_are_at_least_180_s_apart():
    h = Harness(room=24.6)
    h.enable()
    h.sensor_value = 23.0  # "off" wanted 20 s later
    h.run(20)
    assert h.values_written()[-1] == 25  # held: only 20 s since the last new value
    h.run(170)
    assert h.values_written()[-1] == 23  # written once the 180 s are up
    times = [t for t, w in h.writes if w.address == 0x3307 and "repeat" not in w.reason]
    assert all(b - a >= 180 for a, b in zip(times, times[1:]))


def test_flicker_back_to_the_written_value_writes_nothing():
    h = Harness(room=24.6)
    h.enable()
    n = len(h.writes)
    h.sensor_value = 23.0
    h.run(30)
    h.sensor_value = 24.8
    h.run(200)
    assert [w for _, w in h.writes[n:] if w.address == 0x3307] == []


def test_keepalive_interval_is_an_option():
    h = Harness(IFeelConfig(keepalive=120), room=24.6)
    h.enable()
    h.run(600)
    keepalives = [t for t, w in h.writes if w.values == (1,)]
    assert len(keepalives) >= 4 and all(115 <= b - a <= 130 for a, b in zip(keepalives, keepalives[1:]))


# -- rule 6: start hold-back ------------------------------------------------------------------------------------------


def test_start_is_held_while_the_8_minute_timer_runs():
    h = Harness(room=23.0, last_stop_ago=100)  # stopped 100 s ago: timer runs another 380 s
    h.enable()
    assert h.values_written()[-1] == 23
    h.sensor_value = 25.0
    h.run(60)
    assert h.ctrl.status == ifeel.HOLDING_START and h.values_written()[-1] == 23
    h.run(400)
    assert h.values_written()[-1] == 25 and h.ctrl.status == ifeel.ACTIVE
    h.run(10)
    assert h.unit.running
    clean(h)


def test_custom_minimum_off_time():
    h = Harness(IFeelConfig(min_off_minutes=4), room=25.0)
    h.enable()
    h.run(240)  # one run
    h.sensor_value = 23.0
    h.run(200)
    assert not h.unit.running
    stop = h.unit.last_stop
    h.sensor_value = 25.0
    h.report_sensor()
    h.run(600)
    start = h.unit.starts[-1]
    assert 240 <= start - stop < 270


# -- rule 8 and the run caps ------------------------------------------------------------------------------------------


def test_coil_guard_stops_and_releases():
    h = Harness(room=26.0, room_rate=0.0, coil_rate=8.0, coil_floor=4.0)
    h.enable()
    h.run(240)
    assert h.ctrl.status == ifeel.COIL_GUARD
    assert h.values_written()[-1] == 23 and not h.unit.running
    guard = [e for e in h.events if e.kind == "coil_guard"]
    assert guard[0].data["state"] == "active"
    starts = len(h.unit.starts)
    h.run(900)
    assert any(e.data.get("state") == "released" for e in h.events if e.kind == "coil_guard")
    assert len(h.unit.starts) == starts + 1  # started again once released and the 8-minute timer was over
    assert [e.data["state"] for e in h.events if e.kind == "coil_guard"][:3] == ["active", "released", "active"]
    clean(h)


def test_absolute_run_cap():
    h = Harness(room=26.0, room_rate=0.0, drift=0.0, coil_floor=9.0)
    h.enable()
    h.run(44 * 60)
    assert h.unit.running
    h.run(2 * 60)
    assert h.ctrl.status in (ifeel.RUN_CAP, ifeel.HOLDING_START) and not h.unit.running
    assert any(e.kind == "run_cap" for e in h.events)
    h.run(10 * 60)
    assert h.unit.running  # on again after the hold
    clean(h)


# -- rule 5: the remote -----------------------------------------------------------------------------------------------


def test_remote_fan_change_is_taken_over():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    h.unit.remote_press(fan=2)
    h.run(15)
    assert h.ctrl.enabled and h.unit.active and h.unit.source == 0x2000
    assert h.unit.fan == 2


def test_remote_setpoint_change_is_the_new_target():
    h = Harness(room=24.6)
    h.enable()
    h.run(200)
    h.unit.remote_press(setpoint=26)
    h.run(15)
    assert h.ctrl.enabled and h.unit.active
    assert h.values_written()[-1] == 25  # 24.6 <= 26 - 0.5: "off" at setpoint 26 is 25


def test_other_press_is_taken_over():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    h.unit.remote_press()
    h.run(15)
    assert h.ctrl.enabled and h.unit.active and h.unit.mirror == 25


@pytest.mark.parametrize("mode", [0, 4, 5, 2, 3])
def test_remote_mode_change_turns_ifeel_control_off(mode):
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    h.unit.remote_press(mode=mode)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_MODE


def test_remote_ifeel_turns_ifeel_control_off():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    h.unit.remote_ifeel(25)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_IFEEL


def test_a_board_that_keeps_the_remote_value_is_not_supported():
    h = Harness(room=24.6)
    h.unit.no_modbus_ifeel = True  # board 1A0040: IFeel goes on, but with the remote's source and cached value
    h.enable()
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_IFEEL_UNSUPPORTED


def test_remote_ifeel_soon_after_enabling_is_the_remote():
    h = Harness(room=24.6)
    h.enable()
    h.run(5)
    h.unit.remote_ifeel(25)  # 0x3307 shows the remote's value; the IR frame marker is gone by the next poll
    h.run(10)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_IFEEL


def test_expiry_is_not_remote_use():
    h = Harness(IFeelConfig(keepalive=1000), room=24.3)  # keep-alives "missed"
    h.enable()
    h.run(440)
    assert h.ctrl.enabled and "expiry" in h.kinds()
    assert h.unit.active


def test_own_settings_writes_are_not_remote_use():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    h.own_settings(fan=0)
    h.own_settings(setpoint=25)
    h.run(30)
    assert h.ctrl.enabled and h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START)


# -- rule 9 / 11: modes and setpoints from Home Assistant -------------------------------------------------------------


def test_setpoint_change_writes_at_once():
    h = Harness(room=26.8)
    h.enable()
    h.run(30)
    h.own_settings(setpoint=26)
    assert h.values_written()[-1] == 27  # 26.8 >= 26.5: "on" at the new setpoint, outside the 180 s rule


def test_ac_off_from_home_assistant_suspends_and_turn_on_resumes():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    t0 = h.t
    h.own_settings(mode=0)
    assert [(a, v) for _, a, v in writes_after(h, t0) if a in (0x3306, 0x3307)] == [(0x3307, (16,)), (0x3306, (0,))]
    assert h.ctrl.status == ifeel.SUSPENDED_AC_OFF and h.ctrl.enabled
    h.run(600)
    h.own_settings(mode=1)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START)
    assert h.writes[-2][1].values[0] == 1 and len(h.writes[-2][1].values) == 2


def test_remote_turn_on_after_home_assistant_turned_off_stops():
    h = Harness(room=24.6)
    h.enable()
    h.own_settings(mode=0)
    h.run(60)
    h.unit.remote_press(mode=1)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_MODE


def test_fan_mode_suspends():
    h = Harness(room=24.6)
    h.enable()
    h.own_settings(mode=5)
    assert h.ctrl.status == ifeel.SUSPENDED_FAN and not h.unit.active
    h.own_settings(mode=1)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_heat_uses_reversed_values_and_leaves_30():
    h = Harness(mode=2, setpoint=22, room=21.0)
    h.sensor_value = 21.0
    h.run(600)  # the unit heats on its own sensor first: past its stop plus the Heat minimum off time (5 min)
    h.enable()
    assert h.values_written()[-1] == 21  # "on" in Heat = setpoint - 1
    h.run(30)
    h.apply(h.ctrl.request_disable())
    assert [(w.address, w.values) for _, w in h.writes[-2:]] == [(0x3307, (30,)), (0x3306, (0,))]


def test_disable_after_cooling_leaves_16():
    h = Harness(room=24.6)
    h.enable()
    h.run(30)
    h.apply(h.ctrl.request_disable())
    assert [(w.address, w.values) for _, w in h.writes[-2:]] == [(0x3307, (16,)), (0x3306, (0,))]
    assert not h.unit.active and h.ctrl.status == ifeel.OFF


def test_handback_for_unload_keeps_ifeel_control_enabled():
    h = Harness(room=24.6)
    h.enable()
    h.apply(h.ctrl.handback_for_unload())
    assert [(w.address, w.values) for _, w in h.writes[-2:]] == [(0x3307, (16,)), (0x3306, (0,))]
    assert h.ctrl.enabled


# -- rule 7: the room sensor ------------------------------------------------------------------------------------------


def test_short_unavailability_is_tolerated_long_one_stops():
    h = Harness(room=24.6)
    h.enable()
    h.sensor_available = False
    h.report_sensor()
    h.run(90)
    assert h.ctrl.enabled  # within the 2-minute grace
    h.run(60)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_SENSOR
    h.sensor_available = True
    h.run(300)
    assert not h.ctrl.enabled  # no automatic resume (decided)


def test_out_of_range_stops_at_once():
    h = Harness(room=24.6)
    h.enable()
    h.sensor_value = 45.0
    h.report_sensor()
    assert h.ctrl.status == ifeel.STOPPED_SENSOR


def test_stale_sensor_stops():
    h = Harness(IFeelConfig(stale_minutes=30), room=24.6)
    h.enable()
    h.sensor_reports = False
    h.run(29 * 60)
    assert h.ctrl.enabled
    h.run(2 * 60)
    assert h.ctrl.status == ifeel.STOPPED_SENSOR


def test_sensor_removed_stops():
    h = Harness(room=24.6)
    h.enable()
    h.sensor_entity = None
    h.report_sensor()
    assert h.ctrl.status == ifeel.STOPPED_SENSOR_REMOVED


# -- Shabbat --------------------------------------------------------------------------------------------------------


def test_shabbat_stops_and_ifeel_is_kept_off_after_it():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    h.unit.write(0x330B, (0x0A,))
    h.run(15)
    assert h.ctrl.status == ifeel.STOPPED_SHABBAT and not h.ctrl.enabled
    with pytest.raises(IFeelRefused):
        h.enable()
    h.unit.write(0x330B, (0,))  # the unit brings IFeel back by itself
    assert h.unit.active
    h.run(30)
    assert not h.unit.active


SHABBAT_PAUSES = IFeelConfig(shabbat_pauses=True)


def test_with_the_setting_shabbat_pauses_without_writes_and_resumes():
    h = Harness(SHABBAT_PAUSES, room=24.6)
    h.enable()
    h.run(60)
    t0 = h.t
    h.unit.write(0x330B, (0x0A,))
    h.run(15)
    assert h.ctrl.enabled and h.ctrl.status == ifeel.SUSPENDED_SHABBAT
    assert [(a, v) for _, a, v in writes_after(h, t0)] == [(0x3307, (16,)), (0x3306, (0,))]
    assert not h.ctrl.controlling and not h.ctrl.needs_fast_reads
    t1, reads = h.t, h.fast_read_count
    # keep-alive time, sensor and settings changes from Home Assistant: nothing is written during Shabbat (the model
    # runs the compressor all through Shabbat without a floor on the room temperature, hence only 20 minutes)
    h.run(600)
    h.own_settings(setpoint=24)
    h.sensor_value = 30.0
    h.run(600)
    assert writes_after(h, t1) == [] and h.fast_read_count == reads
    assert h.ctrl.status == ifeel.SUSPENDED_SHABBAT
    h.unit.write(0x330B, (0,))
    h.run(15)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START)
    assert h.unit.active and h.unit.source == 0x2000 and h.unit.mirror == h.ctrl.value_written
    clean(h)


def test_with_the_setting_shabbat_from_the_remote_side_pauses_too():
    h = Harness(SHABBAT_PAUSES, room=24.6)
    h.enable()
    h.run(60)
    h.unit.set_shabat(True)  # not written by the integration
    h.run(15)
    assert h.ctrl.status == ifeel.SUSPENDED_SHABBAT
    h.unit.set_shabat(False)
    h.run(15)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_with_the_setting_enable_during_shabbat_waits_for_its_end():
    h = Harness(SHABBAT_PAUSES, room=24.6)
    h.unit.write(0x330B, (0x0A,))
    h.poll()
    n = len(h.writes)
    h.enable()  # also how IFeel control is restored after a restart during Shabbat
    h.run(60)
    assert h.ctrl.enabled and h.ctrl.status == ifeel.SUSPENDED_SHABBAT and len(h.writes) == n
    h.unit.write(0x330B, (0,))
    h.run(15)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_with_the_setting_ac_off_during_shabbat_stays_suspended_after_it():
    h = Harness(SHABBAT_PAUSES, room=24.6)
    h.enable()
    h.run(60)
    h.unit.write(0x330B, (0x0A,))
    h.run(15)
    h.own_settings(mode=0)
    h.run(60)
    assert h.ctrl.status == ifeel.SUSPENDED_SHABBAT
    h.unit.write(0x330B, (0,))  # the unit brings IFeel back by itself
    h.run(30)
    assert h.ctrl.status == ifeel.SUSPENDED_AC_OFF and not h.unit.active
    h.own_settings(mode=1)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_with_the_setting_a_remote_mode_change_during_shabbat_still_stops():
    h = Harness(SHABBAT_PAUSES, room=24.6)
    h.enable()
    h.run(60)
    h.unit.write(0x330B, (0x0A,))
    h.run(15)
    h.unit.remote_press(mode=2)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_MODE


# -- the remote turning the AC off and on (setting) -------------------------------------------------------------------


REMOTE_OFF_ON = IFeelConfig(remote_off_on_keeps=True)


def test_with_the_setting_remote_off_and_on_in_the_same_mode_pauses_and_resumes():
    h = Harness(REMOTE_OFF_ON, room=24.6)
    h.enable()
    h.run(60)
    t0 = h.t
    h.unit.remote_press(mode=0)
    h.run(15)
    assert h.ctrl.enabled and h.ctrl.status == ifeel.SUSPENDED_AC_OFF
    assert [(a, v) for _, a, v in writes_after(h, t0)] == [(0x3307, (16,)), (0x3306, (0,))]
    h.run(600)
    h.unit.remote_press(mode=1, fan=3)  # on again in Cool (other fan: the new settings are kept)
    h.run(15)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START)
    assert h.unit.active and h.unit.source == 0x2000 and h.unit.fan == 3
    clean(h)


@pytest.mark.parametrize("mode", [2, 3, 4, 5])
def test_with_the_setting_remote_on_in_another_mode_stops(mode):
    h = Harness(REMOTE_OFF_ON, room=24.6)
    h.enable()
    h.run(60)
    h.unit.remote_press(mode=0)
    h.run(60)
    h.unit.remote_press(mode=mode)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_MODE


def test_with_the_setting_home_assistant_off_then_remote_on_resumes():
    h = Harness(REMOTE_OFF_ON, room=24.6)
    h.enable()
    h.run(60)
    h.own_settings(mode=0)
    h.run(60)
    h.unit.remote_press(mode=1)
    h.run(15)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_with_the_setting_fan_mode_off_and_on_at_the_remote_stays_suspended():
    h = Harness(REMOTE_OFF_ON, room=24.6)
    h.enable()
    h.own_settings(mode=5)
    h.run(30)
    h.unit.remote_press(mode=0)
    h.run(30)
    assert h.ctrl.status == ifeel.SUSPENDED_AC_OFF
    h.unit.remote_press(mode=5)
    h.run(15)
    assert h.ctrl.enabled and h.ctrl.status == ifeel.SUSPENDED_FAN and not h.unit.active


def test_with_the_setting_enabled_while_off_the_remote_may_turn_on_in_any_mode():
    h = Harness(REMOTE_OFF_ON, mode=0, room=24.6)
    h.enable()
    assert h.ctrl.status == ifeel.SUSPENDED_AC_OFF
    h.run(60)
    h.unit.remote_press(mode=2)  # the mode it was in before is not known
    h.run(15)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_with_the_setting_a_remote_mode_change_while_on_still_stops():
    h = Harness(REMOTE_OFF_ON, room=24.6)
    h.enable()
    h.run(60)
    h.unit.remote_press(mode=2)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_MODE


# -- rule 4: fast reads, lost values and recovery (docs/IFEEL_DESIGN.md, "Fast status reads and lost values") ------


def test_fast_reads_keep_the_value_for_hours():
    h = Harness(room=24.6)
    h.enable()
    h.run(2 * 3600)
    assert "value_lost" not in h.kinds()
    assert h.fast_read_count > 6000  # about one a second while IFeel is on
    assert h.unit.active and h.unit.mirror == h.ctrl.value_written
    clean(h)


def test_no_fast_reads_while_ifeel_is_off_or_suspended():
    h = Harness(room=24.6)
    h.run(60)
    assert h.fast_read_count == 0  # IFeel control off
    h.enable()
    h.own_settings(mode=0)  # suspended: AC off
    n = h.fast_read_count
    h.run(60)
    assert h.fast_read_count == n


def test_a_short_gap_in_the_reads_is_detected_fast_and_recovered():
    h = Harness(room=24.3)  # value 23; the remote's cached value is 18
    h.enable()
    h.run(30)
    h.fast_reads = False  # Home Assistant busy: no reads for a few seconds
    h.advance(6)
    assert h.unit.mirror == 0  # dropped
    h.fast_reads = True
    h.run(5)
    assert "check_now" in h.kinds()
    assert h.kinds().count("recovery") == 1
    assert h.unit.mirror == 23 and h.ctrl.status == ifeel.ACTIVE
    lost = [e for e in h.events if e.kind == "value_lost"][0]
    assert lost.data["written"] == 23 and lost.data["in_use"] == 0
    h.run(120)
    assert h.kinds().count("recovery") == 1 and h.ctrl.enabled


def test_without_reads_two_recoveries_then_ifeel_control_stops():
    h = Harness(room=24.3)
    h.fast_reads = False  # the reads never come (for example a client that cannot keep up)
    h.enable()
    h.run(120)
    assert h.kinds().count("recovery") == 2
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_VALUE_NOT_KEPT
    assert [(w.address, w.values) for _, w in h.writes[-2:]] == [(0x3307, (16,)), (0x3306, (0,))]  # the safe value
    clean(h)


def test_a_loss_the_reads_do_not_explain_stops_after_two_recoveries():
    h = Harness(room=24.3)
    h.enable()
    h.run(30)
    h.unit.erasing = True  # the value is dropped although the block is read every second
    h.unit.value_at = h.t
    h.run(120)
    assert h.kinds().count("value_lost") == 3 and h.kinds().count("recovery") == 2
    assert h.ctrl.status == ifeel.STOPPED_VALUE_NOT_KEPT


def test_a_loss_is_caught_by_the_polls_when_the_cached_value_equals_the_value_written():
    h = Harness(room=24.3, cached_remote=23)  # 0x3307 reads 23 either way: fast detection cannot see it
    h.enable()
    h.run(30)
    h.fast_reads = False
    h.advance(6)
    h.fast_reads = True
    h.run(25)
    assert "check_now" not in h.kinds()
    assert h.kinds().count("recovery") == 1 and h.unit.mirror == 23


def test_recovery_count_starts_again_after_30_s_of_holding():
    h = Harness(room=24.3)
    h.enable()
    for _ in range(3):
        h.run(40)
        h.fast_reads = False
        h.advance(6)
        h.fast_reads = True
    h.run(40)
    assert h.kinds().count("recovery") == 3 and h.ctrl.enabled  # each loss recovered, never two within 30 s


# -- safety gating and the fallback without slave 1 ---------------------------------------------------------------


def test_refused_without_slave1():
    h = Harness(room=24.6)
    h.slave1_usable = False
    h.poll()
    with pytest.raises(IFeelRefused):
        h.enable()
    assert h.ctrl.status == ifeel.REFUSED_NO_SLAVE1


def test_stops_when_slave1_is_lost():
    h = Harness(room=24.6)
    h.enable()
    h.run(30)
    h.slave1_usable = False
    h.run(15)
    assert h.ctrl.status == ifeel.REFUSED_NO_SLAVE1 and not h.unit.active


def test_refused_on_a_coil_sensor_fault():
    h = Harness(room=24.6, idu_fault=3)
    with pytest.raises(IFeelRefused):
        h.enable()
    assert h.ctrl.status == ifeel.REFUSED_COIL_FAULT


def test_override_without_slave1():
    h = Harness(IFeelConfig(allow_without_coil_protection=True), room=26.0, room_rate=0.0, drift=0.0)
    h.slave1_usable = False
    h.poll()
    h.enable()
    assert h.ctrl.override_in_effect
    assert h.values_written()[-1] == 23  # no start in the first 8 minutes (state unknown: act as if just stopped)
    h.run(9 * 60)
    assert h.values_written()[-1] == 25
    start = [t for t, w in h.writes if w.values[-1] == 25][0]
    h.run(12 * 60)
    stop = [t for t, w in h.writes if w.values[-1] == 23 and t > start][0]
    assert 600 <= stop - start <= 615  # the 10-minute override cap
    assert any(w.values[0] == 1 and len(w.values) == 2 and "keep-alive" in w.reason for _, w in h.writes)
    clean(h)


def test_long_run_has_no_violations():
    h = Harness(room=24.6, drift=0.4, room_rate=0.5)
    h.enable()
    for setpoint in (24, 23, 25):
        h.own_settings(setpoint=setpoint)
        h.run(2 * 3600)
    assert h.ctrl.enabled and len(h.unit.starts) >= 6
    clean(h)


# -- Heat (decided 2026-10-03: decisions 2-4) ---------------------------------------------------------------------------


def test_heat_holds_a_start_for_its_minimum_off_time_from_the_first_poll():
    """No stop seen yet (after a restart): starts are held for the Heat minutes from the first poll; bit 10 never sets
    in Heat."""
    h = Harness(mode=2, setpoint=22, room=23.0)  # the unit itself does not heat (23 > setpoint - 1)
    h.sensor_value = 20.0
    h.report_sensor()
    h.enable()
    assert h.ctrl.status == ifeel.HOLDING_START and h.values_written()[-1] == 23  # setpoint + 1 held
    h.run(285)
    assert not h.unit.running and h.ctrl.status == ifeel.HOLDING_START
    h.run(30)
    assert h.values_written()[-1] == 21 and h.unit.running  # "on" once the 5 minutes are over
    clean(h)


@pytest.mark.parametrize("minutes", [3, 5, 8])
def test_heat_holds_a_start_after_a_stop_for_the_set_minutes(minutes):
    h = Harness(IFeelConfig(heat_min_off_minutes=minutes), mode=2, setpoint=22, room=23.0)
    h.sensor_value = 20.0
    h.run(minutes * 60 + 5)
    h.enable()
    h.run(30)
    assert h.unit.running
    h.sensor_value = 24.0  # warm enough: stop
    h.run(300)
    assert not h.unit.running
    stop = h.unit.last_stop
    h.sensor_value = 20.0  # cold again at once: the start waits
    h.run(minutes * 60 + 30)
    assert h.unit.starts[-1] - stop >= minutes * 60
    assert h.unit.starts[-1] - stop < minutes * 60 + 15
    clean(h)


def test_heat_coil_guard_stops_at_the_limit_and_releases_at_50():
    h = Harness(IFeelConfig(heat_coil_limit=65), mode=2, setpoint=22, room=23.0, heat_coil_peak=72.0, room_rate=0.0)
    h.sensor_value = 19.0
    h.run(305)
    h.enable()
    for _ in range(600):
        h.run(1)
        if "coil_guard" in h.kinds():
            break
    guard = [e for e in h.events if e.kind == "coil_guard"]
    assert guard and guard[0].data["state"] == "active" and guard[0].data["coil"] >= 65
    assert h.values_written()[-1] == 23 and h.ctrl.status == ifeel.COIL_GUARD  # setpoint + 1: stop
    t_guard = h.t
    h.run(400)
    assert h.unit.last_stop > t_guard  # stopped (then held for the Heat minimum off time, checked by clean())
    assert [e.data["state"] for e in h.events if e.kind == "coil_guard"][:2] == ["active", "released"]
    released = [e for e in h.events if e.kind == "coil_guard"][1]
    assert released.data["coil"] <= 50
    clean(h)


def test_heat_coil_guard_does_not_trigger_on_a_normal_run():
    h = Harness(mode=2, setpoint=22, room=23.0, room_rate=0.0)  # the coil peaks at 60 °C, as on the unit
    h.sensor_value = 19.0
    h.run(305)
    h.enable()
    h.run(1800)
    assert "coil_guard" not in h.kinds() and h.unit.running
    assert h.ctrl.status == ifeel.ACTIVE
    clean(h)


def test_heat_coil_limit_is_an_option():
    h = Harness(IFeelConfig(heat_coil_limit=60), mode=2, setpoint=22, room=23.0, room_rate=0.0)
    h.sensor_value = 19.0
    h.run(305)
    h.enable()
    h.run(400)
    assert "coil_guard" in h.kinds()


def test_heat_stops_after_one_failed_recovery():
    """A dropped value means "heat" in Heat (rule 4): lost again within 30 s of the first recovery -> stop."""
    h = Harness(mode=2, setpoint=22, room=23.0)
    h.sensor_value = 23.5  # between the switching points: "off" (23)
    h.run(305)
    h.enable()
    h.run(30)
    h.unit.erasing = True
    h.unit.value_at = h.t
    h.run(120)
    assert h.kinds().count("value_lost") == 2 and h.kinds().count("recovery") == 1
    assert h.ctrl.status == ifeel.STOPPED_VALUE_NOT_KEPT
    assert [(w.address, w.values) for _, w in h.writes[-2:]] == [(0x3307, (30,)), (0x3306, (0,))]
    clean(h)


def test_no_keepalive_while_a_loss_is_being_checked():
    h = Harness(room=24.3)
    h.enable()
    h.run(60)
    h.ctrl._keepalive_at = h.t - 1000  # a keep-alive is due
    h.unit.mirror = 0  # the value is dropped: 0x3307 shows the remote's cached value
    n = len(h.writes)
    h.fast_read()  # first read that does not match: a loss is suspected
    assert len(h.writes) == n  # no keep-alive
    h.fast_read()  # second: the cells are read at once and decide -> recovery (the block)
    assert [w.values for _, w in h.writes[n:]][0] == (1, h.ctrl.value_written)
    assert (1,) not in [w.values for _, w in h.writes[n:]]
    clean(h)


# -- Dry (no IFeel in Dry) -----------------------------------------------------------------------------------------


def test_dry_from_home_assistant_suspends_and_cool_resumes():
    h = Harness(room=24.6)
    h.enable()
    h.run(60)
    t0 = h.t
    h.own_settings(mode=4)
    assert [(a, v) for _, a, v in writes_after(h, t0) if a in (0x3306, 0x3307)] == [(0x3307, (16,)), (0x3306, (0,))]
    assert h.ctrl.enabled and h.ctrl.status == ifeel.SUSPENDED_DRY and not h.unit.active
    assert not h.ctrl.controlling and not h.ctrl.needs_fast_reads
    n = len(h.writes)
    h.run(900)
    assert len(h.writes) == n
    h.own_settings(mode=1)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active
    clean(h)


def test_enable_in_dry_waits_for_another_mode():
    h = Harness(mode=4, room=24.6)
    h.enable()
    h.run(60)
    assert h.ctrl.status == ifeel.SUSPENDED_DRY and h.writes == []
    h.own_settings(mode=2)
    assert h.ctrl.status in (ifeel.ACTIVE, ifeel.HOLDING_START) and h.unit.active


def test_dry_and_fan_from_home_assistant_write_nothing():
    h = Harness(mode=4, room=24.6)
    h.enable()
    h.own_settings(mode=5)
    assert h.ctrl.status == ifeel.SUSPENDED_FAN
    h.own_settings(mode=4)
    assert h.ctrl.status == ifeel.SUSPENDED_DRY and h.writes == []


def test_with_the_setting_remote_off_and_on_in_dry_stays_suspended():
    h = Harness(REMOTE_OFF_ON, mode=4, room=24.6)
    h.enable()
    h.unit.remote_press(mode=0)
    h.run(30)
    assert h.ctrl.status == ifeel.SUSPENDED_AC_OFF
    h.unit.remote_press(mode=4)
    h.run(15)
    assert h.ctrl.enabled and h.ctrl.status == ifeel.SUSPENDED_DRY and not h.unit.active


def test_with_the_setting_shabbat_ending_in_dry_keeps_ifeel_off():
    h = Harness(SHABBAT_PAUSES, room=24.6)
    h.enable()
    h.run(60)
    h.unit.write(0x330B, (0x0A,))
    h.run(15)
    h.own_settings(mode=4)
    h.run(60)
    h.unit.write(0x330B, (0,))  # the unit brings IFeel back by itself
    h.run(30)
    assert h.ctrl.status == ifeel.SUSPENDED_DRY and not h.unit.active


# -- Auto removed (owner decision 2026-10-04) ------------------------------------------------------------------------


def test_enable_in_auto_is_refused():
    h = Harness(mode=3, room=24.6)
    with pytest.raises(IFeelRefused) as err:
        h.enable()
    assert err.value.status == ifeel.REFUSED_AUTO and h.ctrl.status == ifeel.REFUSED_AUTO
    assert not h.ctrl.enabled and h.writes == []


def test_with_the_setting_remote_on_in_auto_stops_even_when_the_earlier_mode_is_not_known():
    h = Harness(REMOTE_OFF_ON, mode=0, room=24.6)
    h.enable()  # while off: the earlier mode is not known
    h.run(60)
    h.unit.remote_press(mode=3)
    h.run(15)
    assert not h.ctrl.enabled and h.ctrl.status == ifeel.STOPPED_REMOTE_MODE


# -- the coil as a signed byte; the low-coil guard in Heat too (2026-10-04) ----------------------------------------


def test_heat_low_coil_guard_stops_a_freezing_coil():
    """A frozen coil with the Heat bit set was seen in Auto; in Heat the low-coil guard (the Cool rule) applies too."""
    h = Harness(mode=2, setpoint=22, room=23.0, room_rate=0.0)
    h.sensor_value = 19.0
    h.run(305)
    h.enable()
    h.run(30)
    assert h.unit.running
    h.unit.freeze = True  # the fault: the coil falls instead of rising
    for _ in range(300):
        h.run(1)
        if "coil_guard" in h.kinds():
            break
    guard = [e for e in h.events if e.kind == "coil_guard"][0]
    assert guard.data == {"state": "active", "coil": guard.data["coil"], "limit": "low"} and guard.data["coil"] <= 5
    assert h.values_written()[-1] == 23 and h.ctrl.status == ifeel.COIL_GUARD  # the Heat stop value
    clean(h)
