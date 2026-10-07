# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decoding and write planning (no network, no Home Assistant)."""
import pytest

from custom_components.electra_gemini.registers import (
    DEFAULT_FAN,
    DEFAULT_SETPOINT,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    MODE_OFF,
    REG_MODE,
    REG_SETPOINT,
    Overrides,
    Status,
    WriteRequest,
    fault_text,
    parse_internal,
    parse_status,
    plan_write,
    with_overrides,
    IDU_FAULTS,
    ODU_FAULTS,
)

# A read from the real unit (2026-09-19): Cool, Medium, 24, room 24, ODU fault 1, alarm active
SAMPLE = [1, 1, 24, 24, 0, 1, 0, 30, 0, 0, 160, 0, 0, 0, 0, 0, 0, 0x0A, 0]


def status(mode=MODE_COOL, fan=FAN_MEDIUM, setpoint=24) -> Status:
    return Status(mode, fan, setpoint, 24, 0, 0, False, False, False)


def test_parse_sample():
    s = parse_status(SAMPLE)
    assert (s.mode, s.fan, s.setpoint, s.room_temp) == (1, 1, 24, 24)
    assert (s.idu_fault, s.odu_fault) == (0, 1)
    assert s.alarm and not s.defrost and not s.overflow


def test_parse_invalid_values_become_none():
    regs = list(SAMPLE)
    regs[0], regs[1], regs[2] = 9, 9, 0
    s = parse_status(regs)
    assert (s.mode, s.fan, s.setpoint) == (None, None, None)


def test_parse_short_read():
    with pytest.raises(ValueError):
        parse_status(SAMPLE[:10])


@pytest.mark.parametrize(
    "word, state",
    [
        (0x0204, "ready"),  # Standby
        (0x0304, "ready"),
        (0x0704, "restart_lockout"),
        (0x1304, "start_pending"),
        (0x1704, "start_pending"),  # pre-start while the lockout bit is still set
        (0x9304, "running"),
        (0x9704, "running"),
    ],
)
def test_compressor_states(word, state):
    internal = parse_internal([0x0800, 0, 0, 0, word, 0, 0, 0, 0x1601, 0, (14 << 8) | 24])
    assert internal.compressor_state == state
    assert internal.running == (state == "running")
    assert internal.coil_temp == 14


def test_fault_state_is_the_definition_or_the_number():
    from custom_components.electra_gemini.registers import fault_state

    assert fault_state(IDU_FAULTS, 0) == "OK"
    assert fault_state(IDU_FAULTS, 8) == "No communication"
    assert fault_state(ODU_FAULTS, 11) == "IPM/Compressor fault"
    assert fault_state(ODU_FAULTS, 42) == "42"


def test_writes_applied():
    from custom_components.electra_gemini.registers import REG_FAN, writes_applied

    s = status(MODE_COOL, FAN_HIGH, 25)
    assert writes_applied(s, [(REG_MODE, [MODE_COOL, FAN_HIGH, 25])])
    assert not writes_applied(s, [(REG_MODE, [MODE_COOL, FAN_LOW, 25])])
    assert writes_applied(s, [(REG_SETPOINT, [25])]) and not writes_applied(s, [(REG_SETPOINT, [24])])
    assert writes_applied(status(MODE_OFF), [(REG_MODE, [MODE_OFF])])
    assert writes_applied(s, [(REG_FAN, [FAN_HIGH])])


def test_fault_text():
    assert fault_text(IDU_FAULTS, 0) == "OK"
    assert fault_text(ODU_FAULTS, 1).startswith("OOT/OCT")
    assert fault_text(IDU_FAULTS, 99) == "Unknown fault 99"


# -- planning --------------------------------------------------------------------------------------------------------


def test_turn_off():
    plan = plan_write(status(), Overrides(), WriteRequest(mode=MODE_OFF))
    assert plan.writes == [(REG_MODE, [0])]


def test_turn_off_when_already_off_writes_nothing():
    assert plan_write(status(MODE_OFF), Overrides(), WriteRequest(mode=MODE_OFF)).writes == []


def test_turn_on_uses_one_block_with_current_values():
    plan = plan_write(status(MODE_OFF, FAN_HIGH, 26), Overrides(), WriteRequest(mode=MODE_COOL))
    assert plan.writes == [(REG_MODE, [MODE_COOL, FAN_HIGH, 26])]


def test_turn_on_with_invalid_standby_values_falls_back():
    plan = plan_write(status(MODE_OFF, None, None), Overrides(), WriteRequest(mode=MODE_COOL))
    assert plan.writes == [(REG_MODE, [MODE_COOL, DEFAULT_FAN, DEFAULT_SETPOINT])]


def test_turn_on_with_new_setpoint_and_fan():
    plan = plan_write(status(MODE_OFF), Overrides(), WriteRequest(mode=MODE_DRY, fan=FAN_LOW, setpoint=23))
    assert plan.writes == [(REG_MODE, [MODE_DRY, FAN_LOW, 23])]


def test_fan_or_setpoint_change_in_standby_is_remembered_not_written():
    plan = plan_write(status(MODE_OFF), Overrides(), WriteRequest(setpoint=22))
    assert plan.writes == [] and plan.overrides == Overrides(None, 22)
    plan = plan_write(status(MODE_OFF), plan.overrides, WriteRequest(fan=FAN_HIGH))
    assert plan.writes == [] and plan.overrides == Overrides(FAN_HIGH, 22)
    # ...and sent with the block that turns the unit on
    plan = plan_write(status(MODE_OFF), plan.overrides, WriteRequest(mode=MODE_COOL))
    assert plan.writes == [(REG_MODE, [MODE_COOL, FAN_HIGH, 22])]
    assert plan.overrides == Overrides()


def test_setpoint_alone_is_fc6():
    plan = plan_write(status(), Overrides(), WriteRequest(setpoint=25))
    assert plan.writes == [(REG_SETPOINT, [25])]


def test_fan_change_uses_the_block():
    plan = plan_write(status(), Overrides(), WriteRequest(fan=FAN_LOW))
    assert plan.writes == [(REG_MODE, [MODE_COOL, FAN_LOW, 24])]


def test_mode_change_uses_the_block_with_current_fan_and_setpoint():
    plan = plan_write(status(), Overrides(), WriteRequest(mode=MODE_FAN))
    assert plan.writes == [(REG_MODE, [MODE_FAN, FAN_MEDIUM, 24])]


def test_no_change_no_write():
    assert plan_write(status(), Overrides(), WriteRequest(mode=MODE_COOL, fan=FAN_MEDIUM, setpoint=24)).writes == []


def test_off_wins_over_other_changes_but_they_are_remembered():
    plan = plan_write(status(), Overrides(), WriteRequest(mode=MODE_OFF, setpoint=22))
    assert plan.writes == [(REG_MODE, [0])] and plan.overrides == Overrides(None, 22)


@pytest.mark.parametrize("request_", [WriteRequest(setpoint=15), WriteRequest(setpoint=31), WriteRequest(fan=6), WriteRequest(mode=6)])
def test_invalid_requests_rejected(request_):
    with pytest.raises(ValueError):
        plan_write(status(), Overrides(), request_)


def test_unknown_mode_is_never_written_over():
    with pytest.raises(ValueError):
        plan_write(status(None), Overrides(), WriteRequest(setpoint=25))


def test_merge_latest_wins():
    merged = WriteRequest(mode=MODE_COOL, setpoint=23).merge(WriteRequest(setpoint=25, fan=FAN_LOW))
    assert merged == WriteRequest(MODE_COOL, FAN_LOW, 25)


def test_with_overrides_only_in_standby():
    assert with_overrides(status(MODE_OFF, FAN_LOW, 20), Overrides(FAN_HIGH, 22)).setpoint == 22
    assert with_overrides(status(MODE_OFF, None, None), Overrides()).fan == DEFAULT_FAN
    assert with_overrides(status(MODE_COOL), Overrides(FAN_HIGH, 22)).setpoint == 24


# -- flags -----------------------------------------------------------------------------------------------------------


def test_parse_flags():
    s = parse_status(SAMPLE)
    assert not (s.shabat or s.sleep or s.timer or s.ifeel)
    regs = list(SAMPLE)
    regs[0x0B], regs[0x0E], regs[0x12], regs[0x06] = 0x0A, 0x0A, 0x0A, 1
    s = parse_status(regs)
    assert s.shabat and s.timer and s.sleep and s.ifeel
    regs[0x0B] = 1  # only 0x0A means on
    assert not parse_status(regs).shabat


def test_plan_flags():
    from dataclasses import replace

    from custom_components.electra_gemini.registers import REG_SHABAT, REG_SLEEP

    assert plan_write(status(), Overrides(), WriteRequest(shabat=True)).writes == [(REG_SHABAT, [0x0A])]
    assert plan_write(status(), Overrides(), WriteRequest(sleep=True)).writes == [(REG_SLEEP, [0x0A])]
    on = replace(status(), shabat=True, sleep=True)
    assert plan_write(on, Overrides(), WriteRequest(shabat=True)).writes == []
    assert plan_write(on, Overrides(), WriteRequest(shabat=False, sleep=False)).writes == [
        (REG_SHABAT, [0]),
        (REG_SLEEP, [0]),
    ]
    # climate first, then the flag
    assert plan_write(status(), Overrides(), WriteRequest(setpoint=26, sleep=True)).writes == [
        (REG_SETPOINT, [26]),
        (REG_SLEEP, [0x0A]),
    ]
    # a flag alone does not depend on the mode (not even an invalid one) and keeps the Standby choices
    plan = plan_write(status(None), Overrides(FAN_HIGH, 22), WriteRequest(shabat=True))
    assert plan.writes == [(REG_SHABAT, [0x0A])] and plan.overrides == Overrides(FAN_HIGH, 22)


def test_writes_applied_for_flags():
    from dataclasses import replace

    from custom_components.electra_gemini.registers import REG_SHABAT, writes_applied

    assert not writes_applied(status(), [(REG_SHABAT, [0x0A])])
    assert writes_applied(replace(status(), shabat=True), [(REG_SHABAT, [0x0A])])
    assert writes_applied(status(), [(REG_SHABAT, [0])])


# -- unit 1 structure check ------------------------------------------------------------------------------------------


def test_parse_internal_checks_the_layout_against_the_room_temperature():
    regs = [0x0800, 0, 0, 0, 0x0304, 0, 0, 0, 0x1601, 0, (20 << 8) | 24]
    assert parse_internal(regs, room_temp=24).coil_temp == 20
    assert parse_internal(regs, room_temp=26).coil_temp == 20  # within the tolerance
    with pytest.raises(ValueError):
        parse_internal(regs, room_temp=27)
    with pytest.raises(ValueError):
        parse_internal(regs[:10], room_temp=24)
    with pytest.raises(ValueError):
        parse_internal(regs + [0], room_temp=24)


@pytest.mark.parametrize(
    "bits, mode, ok",
    [
        (0x0800, MODE_COOL, True),
        (0x0400, MODE_DRY, True),
        (0x0200, MODE_FAN, True),
        (0x0400, MODE_COOL, False),  # mode bits do not agree: another layout
        (0x0100, MODE_HEAT, True),  # Heat shows bit 8 (2026-10-03)
        (0x0000, MODE_HEAT, False),
        (0x0800, MODE_HEAT, False),
        (0x0900, MODE_COOL, False),  # bit 8 is checked too (mask 0x0F00)
        (0x1000, 3, True),  # Auto shows bit 12 (2026-10-04)
        (0x0000, 3, False),
        (0x0100, 3, False),
        (0x1800, MODE_COOL, False),  # bit 12 is checked too (mask 0x1F00)
        (0x0200, MODE_OFF, True),  # Standby: not checked
        (0x0100, MODE_OFF, True),  # bit 8 stays set in Standby after Heat
    ],
)
def test_parse_internal_checks_the_mode_bits(bits, mode, ok):
    regs = [bits, 0, 0, 0, 0x0304, 0, 0, 0, 0x1601, 0, (20 << 8) | 24]
    if ok:
        parse_internal(regs, room_temp=24, mode=mode)
    else:
        with pytest.raises(ValueError):
            parse_internal(regs, room_temp=24, mode=mode)


def test_parse_internal_ifeel_cells():
    regs = [0x4800 | 0x0004, 0, 0x2080, 0, 0x9704, 0, 0, 0, 0x1901, 0, (14 << 8) | 24]
    cells = parse_internal(regs, room_temp=24, mode=MODE_COOL)
    assert cells.ifeel_active and cells.ifeel_source == 0x2000 and cells.mirror == 25 and cells.shabat
    assert cells.running and cells.coil_temp == 14


def test_fan_turbo_and_very_low_are_never_written():
    with pytest.raises(ValueError):
        WriteRequest(fan=4).validate()
    with pytest.raises(ValueError):
        WriteRequest(fan=5).validate()


@pytest.mark.parametrize(
    "coil, mode, room, running, defrost, ok",
    [
        (3, MODE_COOL, 24, True, False, True),  # the coldest reading logged
        (0, MODE_COOL, 24, True, False, True),
        (80, MODE_FAN, 24, False, False, True),
        (-24, MODE_COOL, 24, True, False, True),  # a frozen coil (Auto, 2026-10-04) is a real reading
        (-30, MODE_HEAT, 24, True, False, True),
        (-31, MODE_OFF, 24, False, False, False),  # below the range
        (81, MODE_FAN, 24, False, False, False),  # above the range (0-80, decided 2026-10-03)
        (255, MODE_OFF, 24, False, False, False),  # e.g. a negative value or a missing sensor
        (27, MODE_COOL, 24, True, False, True),  # room + 3: within the tolerance
        (28, MODE_COOL, 24, True, False, False),  # warmer than the room while cooling
        (28, MODE_COOL, 24, False, False, True),  # compressor idle: no direction
        (30, MODE_DRY, 24, True, False, True),  # Dry: not checked (up to room + 6 was logged)
        (20, MODE_HEAT, 30, True, False, True),  # Heat: no room comparison (the board reads its own warm air)
        (10, MODE_HEAT, 24, True, True, True),  # defrost: not checked
        (10, 3, 24, True, False, True),  # Auto: direction unknown
        (55, MODE_HEAT, 24, True, False, True),
        (62, MODE_HEAT, 24, True, False, True),  # the highest Heat reading logged (2026-10-03)
        (75, MODE_HEAT, 24, True, False, True),  # above the Heat coil limit: plausible, the guard acts on it
        (45, MODE_OFF, 24, False, False, True),  # e.g. just after a heating run
    ],
)
def test_coil_plausible(coil, mode, room, running, defrost, ok):
    from dataclasses import replace

    from custom_components.electra_gemini.registers import coil_plausible

    s = replace(status(mode), room_temp=room, defrost=defrost)
    assert coil_plausible(coil, s, running) is ok


def test_the_coil_byte_is_signed():
    regs = [0x0800, 0, 0, 0, 0x9704, 0, 0, 0, 0x1601, 0, (0xE8 << 8) | 24]  # 0xE8 = -24 °C
    assert parse_internal(regs, room_temp=24, mode=MODE_COOL).coil_temp == -24
    regs[10] = (62 << 8) | 24
    assert parse_internal(regs, room_temp=24, mode=MODE_COOL).coil_temp == 62


def test_the_heat_side_bit():
    regs = [0x1000, 0, 0, 0, 0x9A04, 0, 0, 0, 0x1601, 0, (40 << 8) | 24]
    assert parse_internal(regs, room_temp=24, mode=3).heat_side
    regs[4] = 0x9704
    assert not parse_internal(regs, room_temp=24, mode=3).heat_side


def test_a_request_for_auto_is_refused():
    from custom_components.electra_gemini.registers import Overrides, WriteRequest, plan_write

    with pytest.raises(ValueError):
        plan_write(status(MODE_COOL), Overrides(), WriteRequest(mode=3))


@pytest.mark.parametrize("request_kw", [{"fan": 0}, {"setpoint": 22}])
def test_fan_and_setpoint_are_refused_while_in_auto(request_kw):
    from custom_components.electra_gemini.registers import Overrides, WriteRequest, plan_write

    with pytest.raises(ValueError):
        plan_write(status(3), Overrides(), WriteRequest(**request_kw))
    plan = plan_write(status(3), Overrides(), WriteRequest(mode=MODE_COOL))  # another mode is fine
    assert plan.writes[0][1][0] == MODE_COOL


# 0x4040-0x4059 laid out as on the real unit, with made-up serial numbers
IDENTITY_REGS = [
    0x4130, 0x4230, 0x3231, 0x3433, 0x3635, 0x3837, 0xFFFF, 0xFFFF, 0x4131, 0x3030, 0x3835, 0x2020, 0x2020, 0x2020,
    0x2020, 0x2020, 0x3020, 0x3930, 0x3231, 0x3433, 0x3635, 0x3837, 0x3039, 0x3538, 0x3037, 0x3137,
]  # fmt: skip


def test_parse_identity():
    from custom_components.electra_gemini.registers import Identity, parse_identity

    assert parse_identity(IDENTITY_REGS) == Identity(
        board_serial="A0B12345678", board_part="1A0058", board_revision="009", idu_serial="1234567890", idu_product="857071"
    )


# The other layout (board 1A0040): a 10-character serial with the part number right after it, " 003" earlier, FFFF last
IDENTITY_REGS_B = [
    0x3141, 0x3332, 0x3534, 0x3736, 0x3938, 0x4131, 0x3030, 0x3034, 0x2020, 0x2020, 0x2020, 0x2020, 0x2020, 0x3020,
    0x3330, 0xFFFF, 0xFFFF, 0xFFFF, 0x3231, 0x3433, 0x3635, 0x3837, 0x3039, 0x3538, 0x3835, 0x3637,
]  # fmt: skip


def test_parse_identity_other_layout():
    from custom_components.electra_gemini.registers import Identity, parse_identity

    assert parse_identity(IDENTITY_REGS_B) == Identity(
        board_serial="A123456789", board_part="1A0040", board_revision="003", idu_serial="1234567890", idu_product="855876"
    )


def test_parse_identity_without_text_leaves_the_board_out():
    from custom_components.electra_gemini.registers import parse_identity

    identity = parse_identity([0xFFFF] * 18 + IDENTITY_REGS[18:])
    assert (identity.board_serial, identity.board_part, identity.board_revision) == (None, None, None)
    assert identity.idu_product == "857071"


def test_parse_identity_leaves_out_a_field_that_is_not_text():
    from custom_components.electra_gemini.registers import parse_identity

    regs = list(IDENTITY_REGS)
    regs[0x4052 - 0x4040] = 0x0001  # not printable
    identity = parse_identity(regs)
    assert identity.idu_serial is None and identity.board_part == "1A0058"
