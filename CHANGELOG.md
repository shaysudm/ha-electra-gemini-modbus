# Changelog

## 0.6.1

* The device page shows the controller board: its part number as the model ID, its barcode as the serial number, and
  its hardware revision together with the indoor unit's product and serial numbers as the hardware version (read once
  at startup; left out when the internal cells cannot be read).

## 0.6.0

* **Auto mode removed.** On this unit Auto keeps the direction it picks at its start, and once froze the indoor coil
  (see the README, "Why there is no Auto mode"). Home Assistant refuses Auto with a clear error. If the AC is put in Auto
  at the remote, it is shown as Auto (with the direction it chose), setpoint and fan changes are refused until another
  mode is chosen, and a warning is logged.
* IFeel control is refused in Auto (`refused_auto`).
* The coil temperature is read as a signed value, so a frozen coil (below 0 °C) is shown and acted on instead of being
  taken for a sensor fault (plausible range -30 to 80 °C).
* The coil freeze guard now also works in Heat. The two coil limits are now called "Coil freeze protection limit" and
  "Coil overheat protection limit in Heat".

## 0.5.0

* **No IFeel in Dry:** setting Dry from Home Assistant suspends IFeel control (`suspended_dry`), like Fan.
* **Heat:** a new option, "Minimum time between compressor stop and start in Heat" (default 5 minutes; the unit has no
  8-minute timer in Heat), and a coil overheat guard (default 65 °C). In Heat, one failed recovery of a lost value stops
  IFeel control (a lost value means "heat" there).
* No keep-alive is sent while a lost value is being checked.
* The internal cells' mode check now covers Heat; normal Heat coil readings (up to about 62 °C) are no longer taken for a
  sensor fault.
* Clearer notes on the compressor-state and board-temperature entities.

## 0.4.1

* Setup checks the internal cells: if they can be read, a second screen chooses the room sensors for IFeel control; if
  not, a warning explains that IFeel control is not available.
* The gateway's address and port can be changed with "Reconfigure".

## 0.4.0

* Two new options (both off by default): "Keep IFeel control when the remote turns the AC off and on" and "Keep IFeel
  control through Shabbat mode" (pause instead of stop; new status `suspended_shabbat`).

## 0.3.0

* While IFeel control is on, the status block is read every second: the unit keeps an IFeel value only while it is read
  at least about every 2 s. A lost value is noticed within about 2 s and re-sent; if the AC does not keep the values,
  IFeel control stops with a notification. A debug log is recorded only for a loss the read rate does not explain.

## Earlier versions

Not published.
