# IFeel control: design

> **Unofficial, independent work, not affiliated with or endorsed by Electra, Airwell or Elfin. Provided "as is",
> without any warranty; use it at your own risk.** Writing to an air conditioner through undocumented registers can
> damage it or affect its warranty. See the [Disclaimer](../README.md#disclaimer) and the [licence](../LICENSE)
> (AGPL-3.0-or-later).

IFeel control lets a Home Assistant temperature sensor control the AC instead of the sensor in the indoor unit, through
the unit's IFeel input (`0x3306` / `0x3307`). This document describes how it works and why. The registers and the
unit's behaviour it relies on are in [`REGISTER_MAP.md`](REGISTER_MAP.md), in particular "IFeel driven over Modbus".

**Status:** tested on the real unit in Cool; the Heat thresholds were tested on the unit with scripts that write the same
registers. Not used in Fan and Dry; Auto is not supported at all. Experimental: use at your own risk.

## Why IFeel

The board's own sensor sits inside the indoor unit and reads the air there, not the room (in Heat it reads the unit's own
warm air). Writing setpoints from Home Assistant to steer the unit would work too, but every setpoint write makes the
wall controller beep. IFeel is the input the remote uses for its own room temperature, so it is the natural place for a
better room reading.

## Thermostat logic

IFeel control works like Home Assistant's generic thermostat. From the room sensor, the target (the unit's setpoint) and
two tolerances (default 0.5 °C each) it decides "on" or "off":

* **Cool:** on at or above setpoint + hot tolerance, off at or below setpoint - cold tolerance.
* **Heat:** on at or below setpoint - cold tolerance, off at or above setpoint + hot tolerance.
* Between the two it keeps the last decision. It starts in "off".

It tells the unit through the IFeel value, using the unit's own thresholds (it starts at setpoint + 1 and stops at
setpoint - 1 in Cool, the reverse in Heat, and holds its state at the setpoint):

| | "on" | "off" |
|---|---|---|
| Cool | setpoint + 1 | setpoint - 1 |
| Heat | setpoint - 1 | setpoint + 1 |

So the unit never sees the room temperature itself, only the decision. The sensor's finer resolution (the unit works in
whole degrees) ends runs sooner than the remote's whole-degree IFeel.

## Control rules

1. **Enable and hand-back.** IFeel is enabled with one block `[1, value]` to `0x3306`-`0x3307`, and the value is written
   again 1.5 s later. Every switch-off leaves a value behind that cannot start the compressor at any setpoint the unit
   allows (16 after cooling, 30 after heating) and then writes `0x3306 = 0` (the "hand-back").
2. **New values at most every 180 s** (fixed; the latest decision wins). Written at once instead: after a setpoint or
   mode change, a safety stop, the end of a start hold-back, and the enable sequence when control starts or resumes.
3. **Keep-alive:** `0x3306 = 1` every 180 s (option, 60-300 s), because Modbus IFeel switches itself off about 7
   minutes after the last enable. Never while a lost value is being checked (a keep-alive does not bring a dropped value
   back; the recovery does).
4. **Verification and recovery** (see "Fast status reads and lost values"): the unit must show IFeel active from Modbus
   with the value written. A lost value is re-sent with the enable sequence at once; lost again within 30 s, it is
   re-sent once more in Cool, and IFeel control stops in Heat; a further loss stops it in Cool too (status
   `stopped_value_not_kept`, with a notification). IFeel found off more than about 400 s after the last enable or
   keep-alive is an expiry, not remote use: it is simply enabled again.
5. **The remote:**
   * a fan speed or setpoint change at the remote: Home Assistant wins with the new settings (IFeel is taken back at once,
     the new setpoint becomes the target, the new fan speed is kept);
   * any other button press that turns IFeel off inside the unit: taken back at once;
   * **a mode change at the remote** (including turning the AC off or on): IFeel control **stops** (`stopped_remote_mode`),
     unless the setting "Keep IFeel control when the remote turns the AC off and on" covers it (see "Settings");
   * **the remote's own IFeel switched on:** IFeel control stops (`stopped_remote_ifeel`).

   The integration's own settings writes are recognised as its own (they are announced before they are written), so they
   are not taken for remote use.
6. **Start hold-back.** While the compressor is idle and a minimum off time is running, a value that would start it is
   held back: the "off" value stays (status `holding_start`), and "on" is written the moment the time is over. A start
   already sent is not taken back, and a stop is never held.
   * **Cool:** while the unit's 8-minute timer (`0x4805` bit 10) is set. Option: a fixed 3-8 minutes from the stop the
     integration saw instead (not recommended).
   * **Heat:** the unit has no 8-minute timer there, so "Minimum time between compressor stop and start in Heat"
     (default 5 minutes, 3-8) from the stop the integration saw; after a restart, when no stop has been seen yet, from
     the first reading. The unit itself waits about 3 minutes; on its own sensor in Heat it restarted after 4.4-6.5
     minutes, so 5 minutes is at least as cautious.
7. **The room sensor:** IFeel control stops (`stopped_sensor`) when the sensor is unavailable for more than 2 minutes,
   reads outside 10-40 °C, or has not reported for 30 minutes (option; "reported" also counts an unchanged value). There
   is no automatic resume. Removing the chosen sensor from the options stops it too (`stopped_sensor_removed`).
8. **Coil guards and run caps** (status `coil_guard` / `run_cap`; a log line, no notification):
   * **Freeze guard (Cool and Heat):** the coil at or below "Coil freeze protection limit" (default 5 °C, 3-10) for 60 s
     while the compressor runs sends the "off" value; released when the coil is above 10 °C, after which the start
     hold-back decides. The unit does not protect its coil in Cool (a long run took it to 3 °C), and in Auto a coil froze
     to -24 °C with the Heat bit set, so the guard covers Heat too (a misfire during a defrost would only stop heating).
   * **Overheat guard (Heat):** the coil at or above "Coil overheat protection limit in Heat" (default 65 °C, 60-75)
     while running sends the "off" value at once; released at or below 50 °C. The unit normally keeps its coil at about
     61 °C by itself, so this is a backstop for a fault.
   * **Maximum run time:** the compressor is stopped after running 45 minutes without a break (option, 15-120).
9. **Modes set from Home Assistant:**
   * **Off, Fan or Dry:** hand-back (rule 1), and the status `suspended_ac_off` / `suspended_fan` / `suspended_dry`; the
     IFeel control switch stays on. Nothing is written while suspended.
   * **Back to Cool or Heat:** the enable sequence, with a fresh on / off decision.
   * **Cool to Heat or back:** the new value at once (the meaning of "on" and "off" flips).
10. **Hand-back** (rule 1) when the user switches IFeel control off, when the integration is unloaded or Home Assistant
    stops. Not on a reload caused by an options change: IFeel stays on and the reloaded integration takes over within
    seconds (if it does not, IFeel expires by itself). A changed connection (Reconfigure) hands back through the old
    connection first.
11. **Setpoint changes** (from Home Assistant or the remote) re-evaluate the decision and write it at once.
12. **The room sensor choice:** the options list the temperature sensors to offer; the "IFeel room sensor" select on the
    device picks one (chosen automatically when only one is offered). To combine rooms, offer a "Combine the state of
    several sensors" helper. The choice and the switch are restored after a restart.
13. **Write allow-list.** The integration's Modbus client refuses any other write: `0x3300`-`0x3302` (1-3 registers),
    `0x330B` and `0x3312` with 0 or `0x0A`, `0x3306` with 0 or 1, `0x3307` with 10-40, and the block `[1, value]` at
    `0x3306` with a value of 10-40. Nothing is ever written to unit 1.

## Modes

| Mode | IFeel control |
|---|---|
| Cool | yes |
| Heat | yes (thresholds mirror Cool) |
| Fan | suspended (no compressor) |
| Dry | suspended. Dry's own logic needs the board sensor: with a held IFeel value its pulses do not end by themselves (see REGISTER_MAP.md, "Modes"), and its benefit over Cool is negligible |
| Auto | **not supported.** Switching IFeel control on in Auto is refused (`refused_auto`); Auto set at the remote stops it. On this unit Auto keeps the direction it picked at its start, and once froze the indoor coil; the integration does not offer Auto at all |
| Off | suspended |

## Fast status reads and lost values

The unit keeps a Modbus IFeel value only while unit 160 is read in blocks at least about every 2 s (REGISTER_MAP.md).
Without such reads it drops the value and runs on 0: in Cool that holds the compressor off, **in Heat it heats
non-stop**. So:

* **While IFeel is on in the unit** (statuses `active`, `holding_start`, `coil_guard`, `run_cap`), the integration reads
  the status block `0x3300` x 19 **every second**. These reads also update the entities. The normal poll (every 10 s,
  option 5-300 s, which also reads unit 1) goes on as before. A failed fast read does not make anything unavailable, and
  a reply of all `0xFFFF` counts as a failed read.
* **Fast detection:** `0x3307` shows the value written while it is in use and the remote's last value once it is
  dropped. Two fast reads in a row that do not show the value written make the integration read the unit-1 cells at once;
  those decide (rule 4). A loss is noticed within about 2 s.
* **The check** (on every poll and after a fast detection): IFeel active (`0x4801` bit 14), from Modbus (`0x4803`), and
  the value in use (`0x4809`) equal to the value written. Outside a recovery, a loss must show on two polls in a row
  unless fast detection asked for the check. A recovery only counts once the value has held for 30 s. A value that
  differs while the remote's frame marker shows a press is explained by the remote (rule 5).
* **Each loss** is one warning in the Home Assistant log, with the longest gap between status reads since the write.
* **A loss although the reads were no more than 2 s apart** is not explained by the read rate. Only then a **debug log**
  is recorded (see "Debug log").

## Without a usable unit 1

Unit 1 is "usable" when it answers and its layout checks out: the low byte of `0x480B` matches the room reading within
2 °C, and the mode bits of `0x4801` match the mode (Standby is not checked). After 3 failed polls in a row it is tried
again every 5 minutes.

Without it, IFeel control is **refused** (`refused_no_slave1`), because the coil cannot be watched. With the override
("Allow IFeel control without coil protection", not recommended) it runs with less protection:

| | With unit 1 | Without (override) |
|---|---|---|
| Verification | the cells (rule 4) | `0x3307` only |
| Keep-alive | `0x3306 = 1` | the block `[1, value]` every time (it also restores a lost value) |
| Remote | presses, mode changes and the remote's IFeel are all seen | mode / fan / setpoint changes are seen on unit 160; the remote's IFeel is not |
| Start hold-back | the compressor state | a cautious timer: after a stop value, assume up to 3.5 minutes more running, then the off time (8 minutes, or the set minutes; the Heat minutes in Heat) |
| Coil guards | yes | no: the maximum run time without coil protection (default 10 minutes, option 5-30) stands in for them |
| IFeel source sensor | off / home_assistant / remote | off / on from `0x3306` |

## Safety gating

IFeel control runs only when unit 1 is usable, the coil reading is plausible and there is no indoor coil sensor fault
(IDU fault codes 1 or 3, since their labels may be swapped). Otherwise it is refused (`refused_no_slave1`,
`refused_coil_fault`) unless the override is set.

The coil reading is a signed byte. It is plausible from -30 to 80 °C and, in Cool with the compressor running, not more
than 3 °C above the room. In Heat it is not compared with the room (the board reads warm air). A reading that is not
plausible makes the coil entity unavailable.

## Shabbat

Shabbat mode (from the remote or Home Assistant) runs the compressor on its own fixed cycle, independent of the
thermostat, and switches Modbus IFeel off inside the unit; switching Shabbat off turns IFeel back on in the unit.

* **By default** Shabbat stops IFeel control (`stopped_shabbat`). When Shabbat ends, IFeel is kept off in the unit
  (`0x3306 = 0` until `0x4801` bit 14 is seen clear, a few tries). Switching IFeel control on during Shabbat is refused.
* **With "Keep IFeel control through Shabbat mode"** it pauses instead (`suspended_shabbat`): the hand-back when
  Shabbat starts, then nothing is written while it lasts (no keep-alives, no fast reads; the sensor and the safety
  gating are not checked). When it ends: Cool or Heat resume with the enable sequence; Off, Fan or Dry keep IFeel off as
  above. A remote mode change during Shabbat still stops it. Switching IFeel control on (or a restart) during Shabbat
  waits for its end.

## Settings

All in the integration's options ("Configure"), section "IFeel control" unless noted:

| Setting | Default | Range |
|---|---|---|
| Room sensors offered | none | temperature sensors |
| Cold tolerance / hot tolerance | 0.5 °C | 0.1-3 |
| Keep-alive interval | 180 s | 60-300 |
| Minimum time between compressor stop and start in Cool | the AC's 8-minute timer | or 3-8 minutes (not recommended) |
| Minimum time between compressor stop and start in Heat | 5 minutes | 3-8 |
| Room sensor silent for | 30 minutes | 5-120 |
| Coil freeze protection limit | 5 °C | 3-10 (lowering it risks the coil icing up) |
| Coil overheat protection limit in Heat | 65 °C | 60-75 |
| Maximum compressor run time | 45 minutes | 15-120 |
| Keep IFeel control when the remote turns the AC off and on | off | on: off at the remote pauses (`suspended_ac_off`); on again at the remote in the same mode resumes, in another mode stops; when the earlier mode is not known (switched on, or restarted, while the AC was off) any mode but Auto resumes |
| Keep IFeel control through Shabbat mode | off | see "Shabbat" |
| *Not recommended:* Allow IFeel control without coil protection | off | see "Without a usable unit 1" |
| *Not recommended:* Maximum compressor run time without coil protection | 10 minutes | 5-30 |

## Entities for IFeel control

* **Switch "IFeel control":** on = the chosen room sensor should drive the AC. It stays on while control is suspended
  or holding a start, and goes off when the user switches it off or control stops. Restored after a restart.
* **Select "IFeel room sensor":** the sensor to use (rule 12).
* **Sensor "IFeel control status":** `off`, `active`, `holding_start`, `coil_guard`, `run_cap`, `suspended_ac_off`,
  `suspended_fan`, `suspended_dry`, `suspended_shabbat`, `stopped_sensor`, `stopped_shabbat`, `stopped_remote_mode`,
  `stopped_remote_ifeel`, `stopped_value_not_kept`, `stopped_sensor_removed`, `refused_no_slave1`,
  `refused_coil_fault`, `refused_auto`. Attributes: the decision and the value in use, the chosen sensor and its value,
  whether the override is in effect, the direction.
* **Climate:** while IFeel control drives the unit, its current temperature is the room sensor's (attribute
  `current_temperature_source`); otherwise the board's reading.
* **Diagnostics:** "IFeel source" (off / home_assistant / remote), "Remote IFeel temperature" (while the remote drives
  IFeel), "Board room temperature".

## Debug log and notifications

* **Notification when IFeel control stops because the AC did not keep the values** (`stopped_value_not_kept`).
* **Debug log:** started only by a loss that the read rate does not explain. It holds the last 30 minutes of polls,
  writes, connection events and events from memory, then everything, in `/config/electra_gemini/ifeel_logs/` (one JSON
  object per line; the last 10 files are kept). It closes 30 minutes after the last such loss, or at 24 hours or 50 MB;
  a notification then gives the file and asks to share it in a GitHub issue. "Download diagnostics" has a summary.

## Not yet tested on the unit

* The integration's Heat rules (the Heat thresholds themselves were tested with scripts), the Heat freeze guard, and
  Heat long-term.
* Defrost (needs cold weather): which cells show it and whether any rule misfires during one.
* The two "Keep IFeel control" settings, and Dry suspend / resume, on the real unit.
