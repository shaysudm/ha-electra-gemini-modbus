# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register map, decoding and write planning for the Electra/Airwell GEMINI IDU. No Home Assistant imports.

See docs/REGISTER_MAP.md. Control/status registers live on unit 160 (0xA0), the internal cells on unit 1.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field, replace

# --- unit 160 -------------------------------------------------------------------------------------------------------
STATUS_ADDRESS = 0x3300
STATUS_COUNT = 19  # 0x3300..0x3312
REG_MODE = 0x3300
REG_FAN = 0x3301
REG_SETPOINT = 0x3302
REG_IFEEL = 0x3306  # IFeel flag: 1 = on, 0 = off (IFeel control writes it; it can read 1 while IFeel is off inside)
REG_IFEEL_TEMP = 0x3307  # IFeel temperature, whole °C (IFeel control writes it; while IFeel is off it shows the
# temperature the remote sent last, the "cached" value)
REG_SHABAT = 0x330B  # 0x0A = on, 0 = off (1 is acknowledged but ignored)
REG_TIMER = 0x330E  # read only here: a timer is set on the remote
REG_SLEEP = 0x3312  # 0x0A = on, 0 = off (1 is acknowledged but ignored)

MODE_OFF, MODE_COOL, MODE_HEAT, MODE_AUTO, MODE_DRY, MODE_FAN = range(6)
FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_AUTO, FAN_TURBO, FAN_VERY_LOW = range(6)

MIN_SETPOINT = 16
MAX_SETPOINT = 30
DEFAULT_FAN = FAN_MEDIUM
DEFAULT_SETPOINT = 24

FLAG_ACTIVE = 0x0A

# --- unit 1 (read only) ---------------------------------------------------------------------------------------------
INTERNAL_ADDRESS = 0x4801
INTERNAL_COUNT = 11  # 0x4801..0x480B
MARKER_ADDRESS = 0x4850  # IR frame marker 0x4850..0x4852: 0xFFFF normally, other values for a few seconds after a
MARKER_COUNT = 3  # command from the remote
IFEEL_ACTIVE = 0x4000  # 0x4801 bit 14: IFeel really on inside the unit (from either source)
IFEEL_SOURCE_MODBUS = 0x2000  # 0x4803 bit 13: IFeel driven over Modbus
IFEEL_SOURCE_REMOTE = 0x1000  # 0x4803 bit 12: IFeel driven by the remote's own IFeel
SHABAT_INTERNAL = (0x0004, 0x0080)  # 0x4801 bit 2, 0x4803 bit 7: Shabbat on (within 0.5 s, before 0x330B)
MODE_BITS_MASK = 0x1F00  # 0x4801 bits 8-12: 0x0800 Cool, 0x0400 Dry, 0x0200 Fan, 0x0100 Heat, 0x1000 Auto
MODE_BITS = {1: 0x0800, 2: 0x0100, 3: 0x1000, 4: 0x0400, 5: 0x0200}
COMPRESSOR_RUNNING = 0x8000
COMPRESSOR_START_PENDING = 0x1000
COMPRESSOR_LOCKOUT = 0x0400
HEAT_SIDE = 0x0800  # 0x4805 bit 11: set in Heat, and in Auto once it has chosen heating (not proof of real heating)
# The low byte of 0x480B equals the room temperature register 0x3303 (within 1 in every sample measured). A larger
# difference means the cells are not laid out as on the unit this was built on, so they are not used at all.
ROOM_CROSSCHECK_TOLERANCE = 2
# Plausible coil temperatures (owner's estimate; 3..32 °C in all 7,515 logged samples). While the compressor runs the
# coil is at or below the room in Cool (never above it in the logs) and at or above it in Heat (assumed, never tested).
# Not checked in Dry: with the running flag set there the coil was up to 6 °C above the room (the flag can be set
# while the compressor barely runs), nor in Auto (direction unknown) or during defrost.
COIL_MIN = -30  # the coil byte is signed: a frozen coil read -24 °C (Auto, 2026-10-04)
COIL_MAX = 80  # in every mode: in Heat the coil runs at 53-62 °C (2026-10-03); above the Heat coil limit (60-75)
COIL_ROOM_TOLERANCE = 3

IDU_FAULTS = {
    0: "OK",
    1: "ICT disconnected/short",
    3: "RAT disconnected/short",
    7: "IDU model not configured",
    8: "No communication",
    9: "No encoder",
    15: "AC power loss",
    21: "Overflow protection",
    24: "FLASH not updated",
    25: "FLASH corrupt",
}
ODU_FAULTS = {
    0: "OK",
    1: "OOT/OCT short (outdoor/coil temp sensor)",
    8: "High pressure",
    9: "Low pressure",
    10: "Drive communication fault",
    11: "IPM/Compressor fault",
    13: "Gas leak",
    14: "Abnormal DC voltage",
    15: "Abnormal AC voltage",
}


def fault_text(table: dict[int, str], code: int) -> str:
    return table.get(code, f"Unknown fault {code}")


def fault_state(table: dict[int, str], code: int) -> str:
    """The definition if the code has one, otherwise the bare number."""
    return table.get(code, str(code))


@dataclass(frozen=True)
class Status:
    """Decoded unit 160 registers 0x3300..0x3312. Mode/fan/setpoint are None if the register holds an invalid value."""

    mode: int | None
    fan: int | None
    setpoint: int | None
    room_temp: int
    idu_fault: int
    odu_fault: int
    defrost: bool
    overflow: bool
    alarm: bool
    shabat: bool = False
    sleep: bool = False
    timer: bool = False
    ifeel: bool = False  # 0x3306 != 0 (can read on while IFeel is off inside the unit)
    ifeel_temp: int = 0  # 0x3307


@dataclass(frozen=True)
class Internal:
    """Decoded unit 1 cells 0x4801..0x480B (plus the IR frame marker if it was read)."""

    running: bool
    start_pending: bool
    lockout: bool  # 0x4805 bit 10: less than 8 minutes since the last stop (a timer only, not a block)
    coil_temp: int | None  # None if the value is not plausible (see coil_plausible); signed (below 0 is real)
    heat_side: bool = False  # 0x4805 bit 11 (HEAT_SIDE)
    ifeel_active: bool = False  # 0x4801 bit 14
    ifeel_source: int = 0  # 0x4803 & 0x3000: IFEEL_SOURCE_MODBUS, IFEEL_SOURCE_REMOTE or 0
    mirror: int = 0  # 0x4809 high byte: the IFeel value in use
    mode_bits: int = 0  # 0x4801 & MODE_BITS_MASK
    shabat: bool = False  # Shabbat on, from the internal cells
    marker: bool | None = None  # IR frame marker seen (None: not read)

    @property
    def compressor_state(self) -> str:
        if self.running:
            return "running"
        if self.start_pending:
            return "start_pending"
        if self.lockout:
            return "restart_lockout"
        return "ready"


COMPRESSOR_STATES = ["running", "start_pending", "restart_lockout", "ready"]


def parse_status(regs: list[int]) -> Status:
    if len(regs) < STATUS_COUNT:
        raise ValueError(f"expected {STATUS_COUNT} registers, got {len(regs)}")
    mode, fan, setpoint = regs[0], regs[1], regs[2]
    return Status(
        mode=mode if MODE_OFF <= mode <= MODE_FAN else None,
        fan=fan if FAN_LOW <= fan <= FAN_VERY_LOW else None,
        setpoint=setpoint if MIN_SETPOINT <= setpoint <= MAX_SETPOINT else None,
        room_temp=regs[3],
        idu_fault=regs[4],
        odu_fault=regs[5],
        defrost=regs[0x330F - STATUS_ADDRESS] == FLAG_ACTIVE,
        overflow=regs[0x3310 - STATUS_ADDRESS] == FLAG_ACTIVE,
        alarm=regs[0x3311 - STATUS_ADDRESS] == FLAG_ACTIVE,
        shabat=regs[REG_SHABAT - STATUS_ADDRESS] == FLAG_ACTIVE,
        sleep=regs[REG_SLEEP - STATUS_ADDRESS] == FLAG_ACTIVE,
        timer=regs[REG_TIMER - STATUS_ADDRESS] == FLAG_ACTIVE,
        ifeel=regs[REG_IFEEL - STATUS_ADDRESS] != 0,  # 1 when on (a written 0x0A also reads back as 1)
        ifeel_temp=regs[REG_IFEEL_TEMP - STATUS_ADDRESS],
    )


def parse_internal(regs: list[int], room_temp: int | None = None, mode: int | None = None) -> Internal:
    """Decode the unit-1 cells 0x4801..0x480B. With room_temp (from 0x3303) and mode (from 0x3300), also check that they
    are laid out as on the unit this was built on; raises ValueError if not:
    * the low byte of 0x480B is the room temperature (within ROOM_CROSSCHECK_TOLERANCE),
    * the mode bits of 0x4801 agree with the mode: every mode shows its own bit (Auto 0x1000, seen 2026-10-04);
      Standby is not checked (bit 8 stays set in Standby after Heat)."""
    if len(regs) != INTERNAL_COUNT:
        raise ValueError(f"expected {INTERNAL_COUNT} registers, got {len(regs)}")
    c4801, c4803, word, c4809, coil = regs[0], regs[2], regs[4], regs[8], regs[10]
    if room_temp is not None and abs((coil & 0xFF) - room_temp) > ROOM_CROSSCHECK_TOLERANCE:
        raise ValueError(
            f"unexpected layout: 0x480B low byte is {coil & 0xFF}, the room temperature is {room_temp}"
        )
    bits = c4801 & MODE_BITS_MASK
    if mode in MODE_BITS and bits != MODE_BITS[mode]:
        raise ValueError(f"unexpected layout: 0x4801 mode bits 0x{bits:04X} for mode {mode}")
    return Internal(
        running=bool(word & COMPRESSOR_RUNNING),
        start_pending=bool(word & COMPRESSOR_START_PENDING),
        lockout=bool(word & COMPRESSOR_LOCKOUT),
        heat_side=bool(word & HEAT_SIDE),
        coil_temp=_signed_byte(coil >> 8),
        ifeel_active=bool(c4801 & IFEEL_ACTIVE),
        ifeel_source=c4803 & (IFEEL_SOURCE_MODBUS | IFEEL_SOURCE_REMOTE),
        mirror=c4809 >> 8,
        mode_bits=bits,
        shabat=bool(c4801 & SHABAT_INTERNAL[0]) or bool(c4803 & SHABAT_INTERNAL[1]),
    )


# --- identity block (slave 1, 0x4040-0x4059; docs/REGISTER_MAP.md, "Identity block") ------------------------------------

IDENTITY_ADDRESS = 0x4040
IDENTITY_COUNT = 26  # read in two parts (at most 22 registers per read)
# ASCII, two characters per register, the LOW byte first. The indoor unit's fields are at fixed places:
_IDU_FIELDS = {
    "idu_serial": (0x4052, 5),  # the indoor unit's serial number (its nameplate)
    "idu_product": (0x4057, 3),  # the indoor unit's product number
}
# The board's fields (0x4040-0x4051, 36 characters) were seen in two layouts:
# * board 1A0058: a type character "0" + an 11-character barcode, FFFF FFFF, the part number, spaces, " 009";
# * board 1A0040: a 10-character serial with the part number right after it, spaces, " 003", FFFF FFFF FFFF.
# So the part number is read as the six characters before the run of spaces, the revision as the digits after it, and
# the serial as the text before the part number (without the type character where a separator comes before the part).
_BOARD_REGS = 18
_PART_LEN = 6


# Every unit-1 address that answered in a full scan of 0x4000-0x4FFF (2026-09-19, 1,877 registers): read for the
# diagnostics snapshot only.
SNAPSHOT_RANGES = ((0x4000, 0x46FF), (0x47FF, 0x4853))
# Characters that are serial numbers, masked in diagnostics: the board's barcode with its type character, the indoor
# unit's serial, and the live cells' copy of 0x4040-0x4047 (0x4813-0x481A; on board 1A0040 it holds the serial too).
SERIAL_ADDRESSES = (
    frozenset(range(0x4040, 0x4046)) | frozenset(range(0x4052, 0x4057)) | frozenset(range(0x4813, 0x481B))
)


@dataclass(frozen=True)
class Identity:
    """Static identifiers; None for a field that does not decode as printable text."""

    board_serial: str | None = None
    board_part: str | None = None
    board_revision: str | None = None
    idu_serial: str | None = None
    idu_product: str | None = None


def _text(regs: list[int]) -> str:
    """The characters of some registers (low byte first); anything not printable becomes NUL."""
    return "".join(chr(b) if 0x20 <= b < 0x7F else "\0" for reg in regs for b in (reg & 0xFF, reg >> 8))


def _board_fields(text: str) -> dict[str, str | None]:
    found: dict[str, str | None] = {"board_serial": None, "board_part": None, "board_revision": None}
    spaces = re.search(r" {2,}", text)  # the padding after the part number
    if spaces is None:
        return found
    head, tail = text[: spaces.start()], text[spaces.end() :]
    revision = re.match(r"\d+", tail)
    found["board_revision"] = revision.group(0) if revision else None
    part = head[-_PART_LEN:]
    if len(part) == _PART_LEN and "\0" not in part:
        found["board_part"] = part
        before = head[:-_PART_LEN]
        if before.endswith("\0"):  # a separator before the part: the serial starts with a type character
            before = before.rstrip("\0")[1:]
        if before and "\0" not in before:
            found["board_serial"] = before
    return found


def parse_identity(regs: list[int]) -> Identity:
    """Decode 0x4040..0x4059 (IDENTITY_COUNT registers)."""
    if len(regs) != IDENTITY_COUNT:
        raise ValueError(f"expected {IDENTITY_COUNT} registers, got {len(regs)}")
    values = _board_fields(_text(regs[:_BOARD_REGS]))
    for name, (address, count) in _IDU_FIELDS.items():
        text = _text(regs[address - IDENTITY_ADDRESS : address - IDENTITY_ADDRESS + count])
        values[name] = text.strip() or None if "\0" not in text else None
    return Identity(**values)


def _signed_byte(value: int) -> int:
    """The coil byte is signed: 0xE8 = -24 °C (a frozen coil, 2026-10-04)."""
    return value - 256 if value >= 128 else value


def marker_seen(regs: list[int]) -> bool:
    """IR frame marker 0x4850..0x4852: anything but 0xFFFF means a command from the remote was received just now."""
    return any(value != 0xFFFF for value in regs)


def coil_plausible(coil: int, status: Status, running: bool) -> bool:
    """Whether a coil reading makes sense for the unit's state (see COIL_MIN/COIL_MAX). The room comparison is made in
    Cool only: in Heat the board's room reading measures the unit's own warm air."""
    if not COIL_MIN <= coil <= COIL_MAX:
        return False
    if running and not status.defrost:
        if status.mode == MODE_COOL and coil > status.room_temp + COIL_ROOM_TOLERANCE:
            return False
    return True


# --- write planning -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteRequest:
    """What the user asked for; None = unchanged. Later requests override earlier ones (see merge)."""

    mode: int | None = None
    fan: int | None = None
    setpoint: int | None = None
    shabat: bool | None = None
    sleep: bool | None = None

    def merge(self, later: WriteRequest) -> WriteRequest:
        return WriteRequest(
            mode=later.mode if later.mode is not None else self.mode,
            fan=later.fan if later.fan is not None else self.fan,
            setpoint=later.setpoint if later.setpoint is not None else self.setpoint,
            shabat=later.shabat if later.shabat is not None else self.shabat,
            sleep=later.sleep if later.sleep is not None else self.sleep,
        )

    @property
    def has_climate(self) -> bool:
        return self.mode is not None or self.fan is not None or self.setpoint is not None

    def validate(self) -> None:
        if self.mode is not None and not MODE_OFF <= self.mode <= MODE_FAN:
            raise ValueError(f"invalid mode {self.mode}")
        if self.mode == MODE_AUTO:
            # removed (owner decision 2026-10-04): on this unit Auto can freeze the indoor coil
            raise ValueError("Auto mode is not supported")
        if self.fan is not None and not FAN_LOW <= self.fan <= FAN_AUTO:
            # Turbo (4) and Very Low (5) are stored by the unit but do nothing (tested 2026-10-01): never written
            raise ValueError(f"invalid fan {self.fan}")
        if self.setpoint is not None and not MIN_SETPOINT <= self.setpoint <= MAX_SETPOINT:
            raise ValueError(f"invalid setpoint {self.setpoint}")


@dataclass(frozen=True)
class Overrides:
    """Fan/setpoint chosen while the unit is in Standby. They are not written (what the unit does with writes to
    0x3301/0x3302 in Standby is untested) but sent with the block that turns the unit on."""

    fan: int | None = None
    setpoint: int | None = None


@dataclass(frozen=True)
class Plan:
    """A list of writes to unit 160: (address, [values]); a single value is sent as FC6, several as FC16."""

    writes: list[tuple[int, list[int]]] = field(default_factory=list)
    overrides: Overrides = Overrides()


def plan_write(status: Status, overrides: Overrides, request: WriteRequest) -> Plan:
    """Decide what to write. Turn on/mode or fan change = one FC16 block [mode, fan, setpoint] at 0x3300;
    turn off = FC6 0x3300 = 0; a setpoint alone = FC6 0x3302; Shabbat/sleep = FC6 0x0A or 0 to their flag, in any
    mode (independent of the climate settings). Nothing is written if nothing would change."""
    request.validate()
    plan = _plan_climate(status, overrides, request) if request.has_climate else Plan(overrides=overrides)
    flags = [
        (address, [FLAG_ACTIVE if wanted else 0])
        for wanted, current, address in (
            (request.shabat, status.shabat, REG_SHABAT),
            (request.sleep, status.sleep, REG_SLEEP),
        )
        if wanted is not None and wanted != current
    ]
    return Plan(plan.writes + flags, plan.overrides)


def _plan_climate(status: Status, overrides: Overrides, request: WriteRequest) -> Plan:
    if status.mode is None:
        raise ValueError("the unit reports an invalid mode, not writing")

    if request.mode == MODE_OFF or (request.mode is None and status.mode == MODE_OFF):
        # Off, or a fan/setpoint change while in Standby: remember it, write at most the switch-off.
        new = Overrides(
            fan=request.fan if request.fan is not None else overrides.fan,
            setpoint=request.setpoint if request.setpoint is not None else overrides.setpoint,
        )
        writes = [] if status.mode == MODE_OFF else [(REG_MODE, [MODE_OFF])]
        return Plan(writes, new)

    if status.mode == MODE_OFF:
        # Turn on: the unit's own fan/setpoint if valid, unless chosen meanwhile.
        fan = _first(request.fan, overrides.fan, status.fan, DEFAULT_FAN)
        setpoint = _first(request.setpoint, overrides.setpoint, status.setpoint, DEFAULT_SETPOINT)
        assert request.mode is not None
        return Plan([(REG_MODE, [request.mode, fan, setpoint])], Overrides())

    if status.mode == MODE_AUTO and request.mode is None:
        # set at the remote: no fan or setpoint writes in Auto (they would write Auto again; owner decision 2026-10-04)
        raise ValueError("the AC is in Auto (set at the remote): choose Cool, Heat, Dry, Fan or Off first")
    mode = request.mode if request.mode is not None else status.mode
    fan = request.fan if request.fan is not None else status.fan
    setpoint = request.setpoint if request.setpoint is not None else status.setpoint
    if fan is None or setpoint is None:
        fan = _first(fan, DEFAULT_FAN)
        setpoint = _first(setpoint, DEFAULT_SETPOINT)
        return Plan([(REG_MODE, [mode, fan, setpoint])])
    if mode == status.mode and fan == status.fan:
        if setpoint == status.setpoint:
            return Plan()
        return Plan([(REG_SETPOINT, [setpoint])])
    return Plan([(REG_MODE, [mode, fan, setpoint])])


def writes_applied(status: Status, writes: list[tuple[int, list[int]]]) -> bool:
    """True if the (raw) status read back shows every value of the writes."""
    actual = {
        REG_MODE: status.mode,
        REG_FAN: status.fan,
        REG_SETPOINT: status.setpoint,
        REG_SHABAT: FLAG_ACTIVE if status.shabat else 0,
        REG_SLEEP: FLAG_ACTIVE if status.sleep else 0,
    }
    return all(actual[address + i] == value for address, values in writes for i, value in enumerate(values))


def _first(*values: int | None) -> int:
    for value in values:
        if value is not None:
            return value
    raise ValueError("no value")


def with_overrides(status: Status, overrides: Overrides) -> Status:
    """Status as the user should see it: while in Standby, show what was chosen for the next turn-on."""
    if status.mode != MODE_OFF:
        return status
    return replace(
        status,
        fan=_first(overrides.fan, status.fan, DEFAULT_FAN),
        setpoint=_first(overrides.setpoint, status.setpoint, DEFAULT_SETPOINT),
    )
