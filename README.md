# Electra GEMINI AC (Modbus) – Home Assistant custom integration

> **Unofficial and experimental. Provided "as is", without any warranty; use it at your own risk.** It controls an air
> conditioner through undocumented registers found by testing a single unit. See [Disclaimer](#disclaimer).

Controls an Electra/Airwell GEMINI IDU through an Elfin EW11 (Modbus TCP -> RTU), shows its diagnostics, and can let a
Home Assistant temperature sensor control the AC through its IFeel input.
Register map: [`docs/REGISTER_MAP.md`](docs/REGISTER_MAP.md). IFeel control design:
[`docs/IFEEL_DESIGN.md`](docs/IFEEL_DESIGN.md).

<p align="center">
  <img src="https://raw.githubusercontent.com/shaysudm/ha-electra-gemini-modbus/main/docs/images/dashboard.jpg"
       alt="Home Assistant's thermostat card for the AC, with IFeel control and the room sensor below it" width="380">
  <br>
  <em>An example card: Home Assistant's thermostat card, with IFeel control and the room sensor (Mushroom cards).</em>
</p>

## Install

**With HACS:** HACS -> the three dots -> Custom repositories -> add this repository's URL with the type "Integration",
then download "Electra GEMINI AC (Modbus)" and restart Home Assistant.

**Manually:**
1. Copy `custom_components/electra_gemini/` to `/config/custom_components/electra_gemini/` (Advanced SSH add-on or File editor).
2. Restart Home Assistant.
3. Settings -> Devices & services -> Add integration -> "Electra GEMINI AC (Modbus)". Enter the EW11 IP address (no default);
   port 8899 and the name "AC" are pre-filled. The Modbus unit ID (160) is fixed and not configurable.
   Note: with the name "AC" the climate entity becomes `climate.ac`; if an entity of that name already exists (for
   example an infrared integration for the same AC), Home Assistant uses `climate.ac_2`, so choose another name.
   The connection is checked (status block, a few tries) and so are the AC's internal status cells (Modbus unit 1).
   If they can be read, a second screen asks for the room sensors to offer for IFeel control (can be left empty).
   If not, a warning says that IFeel control is not available (it can be allowed in the options, "Not recommended",
   at your own risk), and the setup finishes.
4. Options (Configure on the integration): polling interval (default 10 s, 5-300), and the IFeel control settings
   (below).
5. To change the EW11 IP address or port: the entry's menu (three dots) -> Reconfigure. The new connection is checked
   first; the integration then reconnects (IFeel is handed back through the old connection first).

Languages: English, Hebrew and Russian (`translations/`). Fault descriptions and notifications are English only.

Stop any PC script that polls the EW11 first: it accepts at most 3 TCP clients and shares its speed between them.
The EW11 port has no authentication; do not expose it.

## Gateway

The integration was developed and tested with an **Elfin EW11** (Wi-Fi to RS-485), wired to the indoor unit's Modbus
port: see [`docs/WIRING.md`](docs/WIRING.md) for the connections (with photos) and the DIP switch setting (**J2 must be
set to MODBUS**). The settings it needs: UART 9600 baud, 8N1, no flow control, protocol **Modbus** (Modbus TCP on the network side,
converted to Modbus RTU on RS-485), a TCP server (port 8899 by default). The configuration it was developed with is in
[`docs/EW11_reference_config.xml`](docs/EW11_reference_config.xml), **provided as a reference only** (not an importable
backup; the Wi-Fi details and the web login are replaced by placeholders). Change the EW11's factory web login.

Other RS-485 Modbus gateways (Wi-Fi or Ethernet) should work in principle if they convert Modbus TCP to Modbus RTU.
The integration speaks Modbus TCP (MBAP framing) only, so a transparent "raw TCP" bridge, which expects RTU frames from
the client, will not work. **Other gateways are untested**, apart from a Waveshare RS485 to WIFI/ETH adapter that was
tried: communication through it was unreliable with the settings used; this was not investigated further.

## Entities

<p align="center">
  <img src="https://raw.githubusercontent.com/shaysudm/ha-electra-gemini-modbus/main/docs/images/device_page.jpg"
       alt="The device page: device info, controls, the IFeel control status and the diagnostic entities" width="560">
  <br>
  <em>The device page (serial numbers replaced by examples).</em>
</p>

**Device info:** the device is the controller board (GEMINI IDU). With the internal cells (unit 1) usable, its model ID
(part number, e.g. `1A0058`), serial number (barcode) and hardware version (revision) come from the identity block
`0x4040`-`0x4059`, read once at startup; the indoor unit's product and serial numbers are added to the hardware version,
e.g. "009 (indoor unit 857071, S/N 1234567890)". Without unit 1 these fields are left out.

| Entity | Notes |
|---|---|
| Climate | modes off/cool/heat/dry/fan_only (no Auto: see "Why there is no Auto mode"; Auto is shown only while the unit is in it, set at the remote), fan low/medium/high/auto (what the remote offers; the unit stores Turbo and Very Low but ignores them), target 16-30 °C (step 1). Current temperature: the board's own sensor, or the chosen room sensor while IFeel control drives the unit (attribute `current_temperature_source`). In Heat the board's sensor reads the unit's own warm air (several degrees above the room), which is what the unit itself works from |
| Compressor state (diagnostic) | running / start pending / stopped less than 8 min ago / ready. The "8 minutes" is a timer in the unit (`0x4805` bit 10), not a lock: the unit itself only enforces about 3 minutes on and 3 minutes off. Reliable in Cool and Heat; "stopped less than 8 min ago" never shows in Heat (the unit has no 8-minute timer there). Attribute `note` |
| Compressor running (diagnostic) | reliable in Cool and Heat; in Dry the flag can be set while the compressor draws little power; never set in Fan |
| Coil temperature (diagnostic) | °C, medium-high confidence. Unavailable while the value is not plausible: outside -30 to 80 °C (the reading is a signed byte: a frozen coil reads below 0), or, in Cool with the compressor running (not in defrost), more than 3 °C above the room. Heat (the board reads warm air), Dry and Auto are not compared with the room. In Heat the coil normally reaches 53-62 °C; the limits are `COIL_MIN` / `COIL_MAX` in `registers.py` |
| Board room temperature (diagnostic) | `0x3303`, the unit's own sensor inside the indoor unit (whole °C). While heating it measures the unit's own warm air, not the room (attribute `note`) |
| IDU fault, ODU fault (diagnostic sensors) | the fault's definition as the state ("OK" for no fault), the bare number if the code has no definition; the number is also in the `code` attribute (the ODU table is best effort) |
| IDU fault active, ODU fault active, Alarm, Overflow (diagnostic, class problem) | on when the code is non-zero / the flag is active. **On this unit the ODU fault (code 1) and the alarm have been active since day one**, so these two show "problem" right away |
| Defrost (diagnostic) | never seen active |
| Shabbat mode, Sleep mode (switches) | write `0x0A` (on) or `0` (off) to `0x330B` / `0x3312`; work in any mode, including off. The state is the readback (the Shabbat register follows about 3.6 s after a write). **Shabbat mode runs the compressor on its own fixed cycle and stops IFeel control** (or pauses it, with the setting); the start hold-back of IFeel control does not apply in Shabbat mode |
| Timer active (diagnostic) | read only: `0x330E` (a timer set on the remote) |
| IFeel source (diagnostic) | off / home_assistant / remote / unknown, from the unit's internal cells (`0x4801` bit 14, `0x4803`); without them only on / off from `0x3306` (attribute `source_known`), which can read on while IFeel is off inside the unit. Replaces the former "IFeel mode" entity, which is removed |
| Remote IFeel temperature (diagnostic) | the temperature the remote's own IFeel reports; available only while the remote drives IFeel |
| IFeel control (switch), IFeel room sensor (select), IFeel control status (sensor) | see below |

Unit 1 (the internal cells) is optional. It is used only when it answers and its layout checks out: the low byte of
`0x480B` matches the room temperature within 2 °C, and the mode bits of `0x4801` (bits 8-12) agree with the mode (Cool /
Heat / Auto / Dry / Fan each show their own bit; Standby is not checked). Otherwise only the entities based on it are
unavailable; everything else keeps working. The problem is logged once; after 3 failed polls in a row unit 1 is only
tried again every 5 minutes. (Checked against 81,433 logged samples: 3 single mismatches, all during a mode change.)

**Verified on the real unit:** off, cool, dry, fan_only; fan low/medium/high/auto; setpoints 23-25; IFeel control in
Cool. Heat, and IFeel in Heat, were tested with scripts that write the same registers (2026-10-03); the integration's
Heat rules have not been tried on the unit yet.
**Never tested:** defrost (needs cold weather). **IFeel control is not used in Dry. Auto is not offered at all** (below).

### Why there is no Auto mode

Auto was tested on this unit (2026-10-04) and removed:
1. **It doesn't switch direction.** It picks cooling or heating when it starts and keeps it (30-60 minutes on each
   side, never switched), so it adds nothing over choosing Cool or Heat.
2. **It froze the indoor coil once.** Entered from Fan after an hour of Auto cooling, it set the Heat bit and kept the
   indoor fan off, but cooled: the coil reached -24 °C with no airflow, and the unit only stopped after 5 minutes. The
   same request from Standby heated normally.
3. **Its thresholds and its reaction to setpoint changes are unknown.**

If the AC is put in Auto at the remote, Home Assistant shows it (the action from `0x4805` bit 11 and the compressor,
attributes `auto_direction` and `note`, a warning in the log), but refuses setpoint and fan changes until another mode
is chosen, and IFeel control is not available (`refused_auto`; Auto set at the remote while IFeel control is on stops
it).

## IFeel control

Lets a Home Assistant temperature sensor control the AC instead of the sensor in the unit, without the setpoint writes
that make the wall controller beep. The full design, every rule and the reasons are in [`docs/IFEEL_DESIGN.md`](docs/IFEEL_DESIGN.md).

**Setting it up:** in the options, choose the room sensors to offer ("Room sensors offered"; to combine rooms, create a
"Combine the state of several sensors" helper and offer that). On the device page, pick the room in "IFeel room sensor"
(chosen automatically when only one is offered) and switch "IFeel control" on. The choice and the switch are restored
after a restart.

**How it works:** like Home Assistant's generic thermostat, it decides "on" or "off" from the room sensor, the target
(the unit's setpoint) and the cold / hot tolerances (default 0.5 °C), and tells the unit through IFeel values: in Cool
setpoint + 1 means on and setpoint - 1 off, in Heat the reverse (both tested on the unit). **No IFeel in Fan and Dry:** the unit uses its own sensor there (Dry's own logic needs the
board sensor; tested 2026-10-03). A new value is written at once if 3 minutes have passed since the last one, otherwise when they have
(setpoint and mode changes and safety stops go out at once). IFeel is enabled with one block `[1, value]` (and the value
again 1.5 s later) and kept alive with `0x3306 = 1` every 180 s (option). Every switch-off leaves a value behind that
cannot start the compressor at any setpoint (16 after cooling, 30 after heating), then `0x3306 = 0`.

**"IFeel control status"** says what it is doing:
* `active`;
* `holding_start`: a start is held back. In Cool while the unit's 8-minute timer runs (option: a fixed 3-8 minutes
  instead, not recommended); in Heat, where the unit has no such timer, for "Minimum time between compressor stop and
  start in Heat" (default 5 minutes, 3-8) after the stop it saw (after a restart: from the first reading);
* `coil_guard`: in Cool and Heat, the coil at or below the limit (default 5 °C) for a minute: compressor stopped until
  the coil is above 10 °C (in Heat since 2026-10-04: a frozen coil was seen in Auto with the Heat bit set); in Heat
  also the coil at or above "Coil protection limit in Heat" (default 65 °C, 60-75): stopped at once until the coil is
  at or below 50 °C (the unit normally keeps its coil at about 61 °C itself; this is a backstop);
* `run_cap`: the compressor ran 45 minutes (option 15-120);
* `suspended_ac_off` / `suspended_fan` / `suspended_dry`: the AC was turned off or set to Fan or Dry from Home
  Assistant; resumes when it is set to Cool or Heat from Home Assistant;
* `suspended_shabbat`: Shabbat mode, with the setting below;
* why it stopped or was refused (`stopped_*`, `refused_*`). Stopped means the switch is off until the user switches it
  on again.

**What stops it:**
* the room sensor unavailable for more than 2 minutes, outside 10-40 °C, or silent for 30 minutes (option);
* the chosen sensor no longer offered;
* a **mode change at the remote** (including turning the AC off or on, unless the setting below is on) or **the
  remote's own IFeel**; fan or setpoint changes at the remote are taken over (the new setpoint becomes the target) and
  IFeel is taken back at once;
* Shabbat mode (unless the setting below is on);
* the AC not keeping the values (below): status `stopped_value_not_kept`, with a notification.

Two settings (options, both off by default) make some of these pause IFeel control instead:
* **Keep IFeel control when the remote turns the AC off and on:** turning the AC off at the remote pauses it
  (`suspended_ac_off`, as from Home Assistant). Turning it on again at the remote resumes it if the AC comes back in the
  mode it was turned off in; in another mode IFeel control stops (`stopped_remote_mode`). If the earlier mode is not
  known (IFeel control was switched on, or Home Assistant restarted, while the AC was off), any mode resumes.
* **Keep IFeel control through Shabbat mode:** Shabbat mode, from the remote or from Home Assistant, pauses it
  (`suspended_shabbat`): the safe value and `0x3306 = 0` are written when Shabbat starts, then nothing at all until it
  ends (no keep-alives, no fast reads); then IFeel is enabled again (or kept off, if the AC was turned off or set to Fan
  meanwhile). A remote mode change during Shabbat still stops it. Switching IFeel control on (or a restart) during
  Shabbat waits for its end.

**Fast status reads:** the unit keeps a Modbus IFeel value only while its status block is read at least about every
2 s; without the reads it drops the value and runs on 0 (in Cool that holds the compressor off). So while IFeel is on,
the integration reads the status block (`0x3300` x 19) every second; everything else stays at the normal poll. These
reads also update the entities, so a change at the remote is seen within about a second. A reply of all `0xFFFF`
(seen with two clients on the EW11) counts as a failed read.

**A lost value** (Home Assistant busy, a slow or lost connection): the integration checks that IFeel is active and the
value in use is the one written, on every poll and, through `0x3307`, on the fast reads (a loss is noticed within about
2 s). It re-sends the value at once (the enable block: a keep-alive alone does not bring a dropped value back, and none
is sent while a loss is being checked), once more if it is lost again within 30 s, and then stops IFeel control (it
writes the safe value, then `0x3306 = 0`) and shows a notification. **In Heat it stops after one failed re-send:** a
dropped value reads as "very cold" there, so the unit heats non-stop on it (tested 2026-10-03). Each loss is one warning in the Home Assistant log, with
the time since the last status read. A loss although the reads were no more than 2 s apart is not explained by the
above: only then a **debug log** is recorded (the 30 minutes before it from memory, then everything) in
`/config/electra_gemini/ifeel_logs/` (the last 10 files are kept); when it closes, 30 minutes after the last such loss, a
notification gives the file and asks to share it in a GitHub issue. "Download diagnostics" on the integration has a
summary.

**Safety:** IFeel control is refused when the unit's internal cells are not usable or the indoor coil sensor has a
fault (IDU fault 1 or 3, or no plausible coil reading), unless the option "Allow IFeel control without coil protection"
is set. **That option is not recommended and is at your own risk:** it turns off protections (the coil guard, and
without the internal cells also the full IFeel check (only `0x3307` can be compared), detection of the remote's IFeel and the
compressor-based start hold-back, replaced by a cautious timer), and it is unknown whether the unit protects its coil on
its own; the
compressor is then stopped after 10 minutes of running (option 5-30). An options change reloads the integration without
handing IFeel back; unloading it or stopping Home Assistant hands back to the unit.

## How it writes (safety)

* Only these writes to unit 160 are possible (enforced in the client, `modbus.check_write`): `0x3300`-`0x3302` (mode,
  fan, setpoint), the Shabbat (`0x330B`) and sleep (`0x3312`) flags with `0` or `0x0A`, the IFeel flag `0x3306` with 0
  or 1, the IFeel temperature `0x3307` with 10-40, and the IFeel block `[1, value]` at `0x3306`. Nothing else.
* Turn on / mode or fan change: one FC16 block `[mode, fan, setpoint]`; turn off: FC6 `0x3300 = 0`; setpoint alone: FC6 `0x3302`.
* Requests made close together are merged (latest wins), batches are at least 2 s apart, there are no retries and no loops.
  Nothing is written if it would not change anything. The unit enforces its own minimum run and off times (about 3
  minutes each).
* After a write the state shown is a readback, never an assumed value. The unit shows a write a moment after acknowledging it, so
  the readback waits 0.6 s and then repeats (up to about 7.5 s in total) until the value read is the one written; if it never is,
  what the unit reports is shown and a warning is logged.
* While the AC is off, a changed fan speed or setpoint is remembered and sent with the block that turns it on again.
* After a timeout the client waits 2 s before reconnecting; a failed unit-1 read only makes the entities based on it
  unavailable, a failed unit-160 read makes everything unavailable.

## Development

```
pip install pytest-homeassistant-custom-component
pytest                                 # 278 tests (see below)
python simulator/ac_simulator.py       # the simulated unit as a TCP server on 127.0.0.1:8899
python tools/poll_real_ac.py HOST      # read-only poll of the real AC
```

* `tests/test_ifeel.py`: the IFeel controller (`ifeel.py`, no Home Assistant code) against `simulator/unit_model.py`, a
  model of the unit's IFeel input (including the read rate it needs), compressor (3-minute minimums, 8-minute bit), coil, remote and
  Shabbat, on a virtual clock; every scenario checks that each write is on the allow-list and that no start value is
  sent inside the 8-minute window.
* `tests/test_ifeel_ha.py`: IFeel control inside a test Home Assistant against the TCP simulator (entities, writes,
  restore, reload, fast reads, lost values, notifications, the debug log).
* The others: codec, planner, writer, client, config / options flow, entities.

On Windows the Home Assistant test plugin needs stubs for the Unix-only `fcntl` and `resource` modules
(an empty module each in the venv is enough); `tests/conftest.py` switches the plugin's socket blocking off there.

## Disclaimer

This is an independent, unofficial project. It is not affiliated with, authorised, endorsed or supported by Electra,
Airwell, Elfin / Hi-Flying, Home Assistant or Nabu Casa. Product names and trademarks belong to their respective owners
and are used only to describe what the integration works with.

The integration controls an air conditioner through registers that are not publicly documented and were worked out by
testing a single unit. It may behave differently, or not work at all, on other units, models or firmware versions.
Features marked as untested (for example defrost) have never been tried on a real unit. It is unknown whether the unit protects its indoor coil from freezing during long runs.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. YOU USE IT ENTIRELY AT YOUR OWN
RISK. TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE AUTHOR (shaysudm) AND ANY CONTRIBUTORS SHALL NOT BE LIABLE
FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY OF ANY KIND, WHETHER IN CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR ITS USE, INCLUDING BUT NOT LIMITED TO DAMAGE TO THE AIR CONDITIONER, ITS COMPRESSOR OR
OTHER EQUIPMENT, DAMAGE TO PROPERTY, LOSS OF DATA, PERSONAL INJURY, DISCOMFORT OR ENERGY COSTS.

Using third-party software with an air conditioner may affect the manufacturer's warranty. The full disclaimer of
warranty and limitation of liability are in sections 15 and 16 of the licence ([`LICENSE`](LICENSE)).

## License

Copyright (C) 2026 shaysudm.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
later version (`AGPL-3.0-or-later`). The full text is in [`LICENSE`](LICENSE).

Every source file carries an SPDX header; [`REUSE.toml`](REUSE.toml) covers the files that cannot (JSON, Markdown).
In short, and not as a replacement for the licence: you may use, study, change and share it; if you share it, or let
others use a modified version over a network, you must offer them the corresponding source code under the same licence.
