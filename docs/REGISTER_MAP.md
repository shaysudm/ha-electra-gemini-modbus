# Electra / Airwell GEMINI IDU: Modbus register map

> **Unofficial, independent work, not affiliated with or endorsed by Electra, Airwell or Elfin. Provided "as is",
> without any warranty; use it at your own risk.** Writing to an air conditioner through undocumented registers can
> damage it or affect its warranty. See the [Disclaimer](../README.md#disclaimer) and the [licence](../LICENSE)
> (AGPL-3.0-or-later).

This is the register map used by the integration. It is **unofficial**: it was worked out by testing a single unit (an
Electra indoor unit with a GEMINI controller board, reached through an Elfin EW11 Wi-Fi to RS-485 gateway), and it may
differ on other units, models or firmware versions. Write only what is documented below as writable; the unit's
configuration area on unit 1 must never be written.

Confidence is high unless a note says otherwise. Temperatures are whole °C everywhere: the unit has no finer reading in
any register.

## Connection

| Parameter | Value |
|---|---|
| Bus | RS-485, Modbus RTU, 9600 baud, 8N1 |
| Gateway | Elfin EW11 in Modbus TCP to RTU mode (MBAP framing on the network side); TCP server, default port 8899, up to 3 clients. Reference configuration (not an importable backup): [`EW11_reference_config.xml`](EW11_reference_config.xml) |
| Unit IDs | **160 (`0xA0`)**: control and status. **1**: internal cells, read only. 2 and 3 answer but expose nothing useful (3 acknowledges control writes without applying them). Every other ID gives no reply |
| Function codes | 3 (read holding registers), 6 (write one register), 16 (write several). 1, 2 and 4 return exception 1 |

**Security:** the EW11's TCP port has no authentication. Anyone who can reach it can read and write the AC, including
the unit-ID register `0x330A`. Keep the gateway on a protected network.

### Read block behaviour

* **At most 22 registers per read.** 23 or more get no reply (a timeout, not an exception). The whole unit-160 map fits
  in one read: `0x3300`, count 19.
* **The board checks only the last address of a read** (`start + count - 1`). A read that starts on unmapped
  addresses still succeeds and returns 0 there; a read that ends past the map (for example `0x3300` count 20) fails with
  exception 2. So block reads cannot be used to discover registers: scan with count 1.
* **After a request that got no reply, wait about 2 s** before the next one (or reconnect). Otherwise a late reply can be
  matched to the next request.
* Each request takes about 0.1 s, whatever its size; the gateway handles requests one at a time. Two TCP clients can
  poll together (each at about half the speed), but then roughly 1 read in 15 came back as all `0xFFFF`: treat an
  all-`0xFFFF` reply from a block that is not normally `0xFFFF` as a failed read.

## Unit 160 (`0xA0`): control and status

| Address | Name | R/W | Values |
|---|---|---|---|
| `0x3300` | Mode | R/W | 0 Standby (off), 1 Cool, 2 Heat, 3 Auto, 4 Dry, 5 Fan |
| `0x3301` | Fan speed | R/W | 0 Low, 1 Medium, 2 High, 3 Auto. 4 (Turbo) and 5 (Very Low) are stored but do nothing: the unit keeps running the previous speed |
| `0x3302` | Setpoint | R/W | 16-30 °C |
| `0x3303` | Room temperature | R | °C, from the board's own sensor inside the indoor unit (see the notes) |
| `0x3304` | Indoor unit (IDU) fault code | R | see "Fault codes" |
| `0x3305` | Outdoor unit (ODU) fault code | R | see "Fault codes" |
| `0x3306` | IFeel flag | R/W | 0 off, 1 on (`0x0A` reads back as 1). Does **not** reliably show whether IFeel is active: see "IFeel driven over Modbus" |
| `0x3307` | IFeel temperature | R/W | °C, 10-40 accepted. Shows the last Modbus value while it is in use, otherwise the remote's last IFeel temperature |
| `0x3308`, `0x3309` | unknown | | always 0 |
| `0x330A` | Unit ID (slave address) | R/W | 160. **Do not write** |
| `0x330B` | Shabbat mode | R/W | 0 off, `0x0A` on |
| `0x330C`, `0x330D` | unknown | | always 0 |
| `0x330E` | Timer active | R/W | 0 / `0x0A`: a timer is set on the remote (both timers share it) |
| `0x330F` | Defrost | R | 0 / `0x0A` (never seen active; needs cold weather) |
| `0x3310` | Overflow | R | 0 / `0x0A` |
| `0x3311` | Alarm | R | 0 / `0x0A` |
| `0x3312` | Sleep mode | R/W | 0 off, `0x0A` on |

A scan of every address `0x0000`-`0xFFFF` found only `0x3300`-`0x3312` and one stray address, `0x310E` (always 0,
purpose unknown).

### Writing

* **Turn on, or change the mode or the fan:** one FC16 block `[mode, fan, setpoint]` at `0x3300`.
* **Standby:** FC6 `0x3300 = 0`. Fan and setpoint are kept.
* **Setpoint alone:** FC6 `0x3302`.
* Writes are acknowledged in about 0.15 s. The registers show the new value 0.1-3 s later (the fan up to about 3 s).
* A write never starts or stops the compressor directly; the unit's own logic and timers decide (see "Compressor").
* Swing (louvre direction) is not available over Modbus.

### Shabat, timer and sleep flags

* Only `0x0A` switches a flag on; writing 1 is acknowledged but ignored. Writing 0 always switches it off. The flags do
  not affect one another.
* **Shabbat (`0x330B`):** the register follows a write only after about 3.6 s (the internal cells on unit 1 change within
  0.5 s). Shabbat mode runs the compressor on its own fixed cycle, independent of the thermostat: switching it on was
  followed by a start request within a second (2 of 2 times), it switches Modbus IFeel off inside the unit, and switching
  it off turns IFeel back on.
* **Timer (`0x330E`):** a flag only; it has no counterpart on unit 1.
* **Sleep (`0x3312`):** sets bit 11 of `0x480A` on unit 1. Its effect on the setpoint over hours was not measured.

### Room temperature (`0x3303`)

* Whole degrees, from the board's sensor inside the indoor unit. It equals the low byte of `0x480B` on unit 1 (with a
  short lag).
* **In Heat it measures the unit's own warm air** (it rose from 24 to 30 °C in the first 30 s of heating), so it is not
  a room temperature while heating.
* In Dry, with the indoor fan stopped between pulses, it reads the still air near the coil.

### Fault codes

**IDU fault (`0x3304`)**

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | ICT disconnected / short |
| 3 | RAT disconnected / short |
| 7 | IDU model not configured |
| 8 | No communication |
| 9 | No encoder |
| 15 | AC power loss |
| 21 | Overflow protection |
| 24 | Flash not updated |
| 25 | Flash corrupt |

Codes 1 and 3 may be swapped (older service manuals list 1/2 as the room sensor and 3/4 as the indoor coil sensor).

**ODU fault (`0x3305`)** (best effort: the table appears renumbered relative to older service manuals)

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | OOT / OCT short (outdoor / coil temperature sensor) |
| 8 | High pressure |
| 9 | Low pressure |
| 10 | Drive communication fault |
| 11 | IPM / compressor fault |
| 13 | Gas leak |
| 14 | Abnormal DC voltage |
| 15 | Abnormal AC voltage |

## IFeel driven over Modbus

IFeel makes the unit control on a temperature it is given instead of its own sensor. The remote normally sends it by
infrared every 240 s; over Modbus the master writes it to `0x3307`.

**Enable:** write the block `[1, value]` to `0x3306`-`0x3307` (FC16), then the value again to `0x3307` about 1.5 s later.
The value is in use within about 1.2 s. (Enabling with `0x3306 = 1` alone first resets the value in use to 0 for a
moment, unless IFeel was on only seconds before.)

**What the unit does with the value** (the same reaction as to the remote; about 1-3 s to a start, 1-5 s to a stop):

| Mode | Starts at | Stops at | At the setpoint |
|---|---|---|---|
| Cool | value >= setpoint + 1 | value <= setpoint - 1 | holds the current state (idle or running) |
| Heat | value <= setpoint - 1 | value >= setpoint + 1 | holds the current state |
| Dry | Dry's own rule applied to the value (see "Dry") | | |

**The read rate: the value is kept only while the master keeps reading.** The unit keeps a Modbus IFeel value only while
unit 160 is read **in blocks of 8 or more registers at least about every 2 s**, the first read within about 2.5 s of the
write. Reads of 1 or 2 registers, or of unit 1, do not count. Otherwise the unit drops the value:

* the value in use becomes **0** (`0x4809` high byte), and `0x3307` shows the remote's last value, while IFeel still
  shows as active from Modbus (`0x4801` bit 14 set, `0x4803` = `0x2000`);
* the unit really runs on 0: **in Cool it holds the compressor off; in Heat it heats non-stop** (0 is "very cold");
* a dropped value does not come back when the reads resume. A keep-alive (`0x3306 = 1`) does not bring it back either;
  the block `[1, value]` does, within about 0.5 s.

**Expiry and keep-alive:** Modbus IFeel switches itself off about 423 s after the last enable. Writing `0x3306 = 1`
while IFeel is on re-arms the timer without disturbing the value (a keep-alive). After an expiry `0x3306` still reads 1;
writing `0x3306 = 1` brings IFeel back with the last value written over Modbus.

**Disable:** `0x3306 = 0`. It sticks unless the remote's own IFeel is on (its next frame turns IFeel on again).

**Which source is active:** use unit 1, not `0x3306`: `0x4801` bit 14 = IFeel on (from either source), `0x4803`
`0x2000` = Modbus IFeel, `0x1000` = the remote's IFeel, and `0x4809` high byte = the value in use.

**The remote:**

* Any button press on the remote switches Modbus IFeel off inside the unit (the value in use goes back to the remote's
  last value) while `0x3306` keeps reading 1. Any Modbus IFeel write, even `0x3306 = 1` alone, takes it back with the
  last Modbus value.
* With the remote's own IFeel on, each of its frames (every 240 s) takes IFeel over with the remote's temperature
  (`0x4803` = `0x1000`), and any Modbus IFeel write takes it back until the next frame.
* An idle remote sends nothing.
* Each frame received from the remote shows briefly in `0x4850`-`0x4852` (see unit 1).

**Other writes:** settings writes over Modbus (mode, fan, setpoint, Standby, turn-on, Fan mode, sleep) leave Modbus IFeel
on inside the unit. **Shabbat on switches it off** (and Shabbat off switches it back on). IFeel stays on through Standby.

## Compressor

* **Minimum run about 180 s:** a stop request earlier in a run is held until then.
* **Minimum off time about 180 s:** a start request earlier is held until then (in Cool, Heat and Dry).
* `0x4805` bit 10 (the "8-minute" bit) is **only a timer**, set for 480 s after each stop in Cool and Dry. It does not
  block a start (a start request 5 minutes after a stop started the compressor at once). It is **never set in Heat**.
* On its own sensor in Cool the logged cycles restarted about 11 minutes after a stop (the 8-minute timer, then
  2-3.5 minutes more); restarts 5-7 minutes after a stop were also seen.
* Turning the unit on does not start the compressor at once (about 40 s later when it was off for a long time).

## Modes

* **Cool:** the unit does **not** protect its indoor coil on a long run (a 44-minute run took the coil reading to 3 °C;
  the defrost flag never set).
* **Heat:** behaves as Cool in reverse (see the IFeel table; stops take about 5 s). The coil reaches about 53-62 °C, and
  the unit limits it itself at about 61 °C (it withdraws its demand and keeps heating at a lower output). Bit 10 is never
  set. The board's room reading is the unit's own warm air.
* **Dry:** with the board reading above the setpoint, a cooling run down to the setpoint, then a 15-minute pause. At or
  below the setpoint, short compressor pulses (12-24 s, about every 3.3 minutes, the indoor fan on Low only during a
  pulse), each ended when the board reading reaches setpoint + 1 as the fan draws in room air; a pulse that cannot end
  that way ran to about 6 minutes. The pulses remove little moisture; the cooling runs do. With IFeel, the unit applies
  the same rule to the IFeel value, so pulses do not end by themselves (a held value never rises): the integration does
  not use IFeel in Dry.
* **Fan:** never runs the compressor; `0x4805` stays `0x0304`.
* **Auto (not recommended):** it picks cooling or heating when it starts and keeps that direction (30-60 minutes on each
  side, never switched). Once, entered from Fan after an hour of Auto cooling, it set the Heat bit and held the indoor
  fan off but ran in the cooling direction: **the indoor coil froze to -24 °C with no airflow** until the unit stopped
  after 5 minutes. The same request from Standby heated normally. The integration does not offer Auto.
* **Standby:** `0x3300 = 0`. IFeel and the settings are kept.

## Unit 1: internal cells (read only)

| Range | Contents |
|---|---|
| `0x4000`-`0x40D8` | Identity and parameter data (mostly static), including the identity block and the settings words |
| `0x40D9`-`0x46FF` | `0xFFFF` / `0` filler (erased flash) |
| `0x4700`-`0x47FE` | no reply |
| `0x47FF`-`0x4853` | **live cells** |
| `0x4854`- | no reply |

**Do not write to unit 1.** Much of it looks like flash-backed configuration; unit 1 rejects the control writes that
were tried.

### Live cells used by the integration (`0x4801`-`0x480B`)

| Cell | Meaning |
|---|---|
| `0x4801` bit 14 (`0x4000`) | IFeel on (from the remote or Modbus) |
| `0x4801` bits 8-12 | Mode: `0x0800` Cool, `0x0100` Heat, `0x1000` Auto, `0x0400` Dry, `0x0200` Fan. Standby keeps the last mode's bit |
| `0x4801` bit 2 (`0x0004`) | Shabbat on (changes before `0x330B`) |
| `0x4802` | High byte: fan in use (`0x00` Low, `0x01` Medium, `0x02` High, `0x04` Auto, `0x03` off); low byte: setpoint - 15 |
| `0x4803` bits 12-13 | IFeel source: `0x2000` Modbus, `0x1000` the remote, 0 off. Bit 7 (`0x0080`): Shabbat |
| `0x4805` | Compressor word, below |
| `0x4806` | Low byte: a compact copy of `0x4805` (`0x80` running, `0x10` demand, `0x08` Heat, `0x04` the 8-minute timer, `0x01` fan) |
| `0x4809` | High byte: **the IFeel value in use**; low byte: fan speed (2 bits: Turbo and Very Low read as Low and Medium) |
| `0x480A` | 2 x setpoint (half degrees); bit 11 (`0x0800`): sleep mode |
| `0x480B` | High byte: **indoor coil temperature, a signed byte** (`0xE8` = -24 °C); low byte: the board's room reading (as `0x3303`) |
| `0x480C` high byte, `0x480D` low byte | Raw board and coil sensor values (they fall as the temperature rises; one raw value per whole degree) |

**Compressor word `0x4805`:**

| Bit | Meaning |
|---|---|
| 15 (`0x8000`) | Running. Matched the house power meter at every start and stop in Cool and Heat; in Dry it was once seen set while the compressor drew little power. Never set in Fan |
| 12 (`0x1000`) | Demand / start pending: set 1-10 s before a start and through a run while the unit wants it; cleared while a run continues only to reach its minimum |
| 11 (`0x0800`) | Heating side: set in Heat, and in Auto once it has chosen heating (this was also set in the frozen-coil Auto run, so it shows the indoor side's choice, not proof of heating) |
| 10 (`0x0400`) | The 8-minute timer (Cool, Dry; never in Heat) |
| 8 (`0x0100`) | Indoor fan on (clear in Standby, in Dry between pulses, and at the start of a Heat run until the coil is warm) |

Typical values: Standby `0x0204`; Cool idle `0x0304`, idle within 8 minutes of a stop `0x0704`, start pending `0x1304` /
`0x1704`, running `0x9704`; Dry as Cool with bit 8 clear; Heat idle `0x0A04` (fan off) / `0x0B04` (fan on), start pending
`0x1A04`, running `0x9A04` / `0x9B04`, demand withdrawn at the coil limit `0x8B04`; Fan `0x0304` always.

### Other cells

| Cell | Meaning |
|---|---|
| `0x4005` / `0x4025` | The stored settings, `(fan << 12) \| ((setpoint - 15) << 8) \| (mode << 4)`, and its bitwise complement (updated up to about 10 s after a change) |
| `0x4006` / `0x4026` | `0x46B9` / `0xB946` when on, `0xFFFF` / `0x0000` in Standby |
| `0x4004` bit 8 | Stored Shabbat bit (follows about 6 s after a change) |
| `0x481B`, `0x481D` | Further copies of the compressor state and of the mode / IFeel state |
| `0x4820`-`0x482B` | Free-running uptime counters (32-bit pairs at 4.5-12.8 Hz), not a clock |
| `0x4850`-`0x4852` | **Remote frame marker:** `0xFFFF` normally; for a moment after each infrared frame from the remote (a button press, or an IFeel frame every 240 s) `0x4850` holds the frame word and `0x4851` / `0x4852` read `0` / `0x4001`. The frame word's low byte is `0x28` for a button press and `0x68` for an IFeel frame; its high byte carries the setpoint or the temperature (not fully decoded). Modbus writes never set it |

### Identity block

`0x4040`-`0x4059` hold five identifiers as ASCII text, two characters per register, **the low byte of each register
first**. They were matched against the controller board's sticker and the indoor unit's nameplate. Two layouts of the
board's part (`0x4040`-`0x4051`) are known; the indoor unit's fields are the same in both.

Layout A (board 1A0058):

| Registers | Contents | Example |
|---|---|---|
| `0x4040`-`0x4045` | One length / type character, then the controller board's barcode (its serial number) | `0` + `X0X00000000` |
| `0x4046`-`0x4047` | Separator | `0xFFFF 0xFFFF` |
| `0x4048`-`0x404A` | The board's part number | `1A0058` |
| `0x404B`-`0x404F` | Padding | spaces |
| `0x4050`-`0x4051` | One space, then the board's hardware revision | ` 009` |
| `0x4052`-`0x4056` | The indoor unit's serial number | `0000000000` |
| `0x4057`-`0x4059` | The indoor unit's product number | `857071` |

Layout B (board 1A0040):

| Registers | Contents | Example |
|---|---|---|
| `0x4040`-`0x4044` | The board's serial number (10 characters, no type character) | `0000000000` |
| `0x4045`-`0x4047` | The board's part number | `1A0040` |
| `0x4048`-`0x404C` | Padding | spaces |
| `0x404D`-`0x404E` | One space, then the board's revision (on the board seen, `003` against `[004]` on its sticker) | ` 003` |
| `0x404F`-`0x4051` | Not written | `0xFFFF` x 3 |
| `0x4052`-`0x4059` | The indoor unit's serial and product numbers, as in layout A | `0000000000`, `855876` |

In both, the part number is the 6 characters before the run of spaces and the revision the digits after it; the serial
is the text before the part number (in layout A without its leading type character). The integration reads it that
way. The block needs two reads (26 registers). Nothing after `0x4059` decodes as text.

The live cells `0x4813`-`0x481A` repeat `0x4040`-`0x4047` (`0xFFFF` read as `0`), so they hold the board's serial number
too (and in layout B the first characters of the part number).

## Example (pymodbus)

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient(host="GATEWAY_ADDRESS", port=8899, timeout=5)  # the default (socket) framer
client.connect()
UNIT = 0xA0

regs = client.read_holding_registers(address=0x3300, count=19, device_id=UNIT).registers
mode, fan, setpoint, room = regs[0:4]

client.write_registers(address=0x3300, values=[1, 1, 24], device_id=UNIT)  # on: Cool, Medium, 24 °C
client.write_register(address=0x3300, value=0, device_id=UNIT)  # Standby
client.close()
```

## Sources

The mode, fan, setpoint, room, fault, defrost, overflow, alarm and unit-ID registers match the public
[electra-star-modbus](https://github.com/mrserii/electra-star-modbus) project (an Electra "Star" board). Everything else
(IFeel, Shabbat, timer, sleep, `0x310E`, unit 1, and all behaviour) comes from testing this unit.
