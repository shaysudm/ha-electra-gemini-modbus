# SPDX-FileCopyrightText: 2026 shaysudm
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Debug log for IFeel values the unit dropped although it was read often enough (docs/IFEEL_DESIGN.md, "Fast status
reads and lost values" and "Debug log and notifications"). No Home Assistant imports.

The unit drops a Modbus IFeel value when slave 160 is not read in blocks at least about every 2 s. A loss after a longer
gap is explained (the coordinator logs one warning line). A loss although the reads were no more than 2 s apart is not,
and only that starts this log:
* the last 30 minutes of polls, writes, connection events and events are always kept in memory;
* at an unexplained loss a file is started with that history, then everything is appended;
* it closes 30 minutes after the last unexplained loss, or at 24 hours / 50 MB, and the caller then notifies the user;
* the last 10 files are kept. One JSON object per line.
The file writes are plain functions (write_lines, prune) for the caller to run in an executor.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HISTORY = 30 * 60.0
QUIET_AFTER = 30 * 60.0
MAX_SECONDS = 24 * 3600.0
MAX_BYTES = 50 * 1024 * 1024
KEEP_FILES = 10
PREFIX = "ifeel_"


@dataclass
class LogSummary:
    path: str
    started: str
    ended: str | None = None
    unexplained_losses: int = 0
    reason: str | None = None


@dataclass
class DebugLog:
    directory: Path
    clock: Callable[[], float] = time.time
    ring: deque = field(default_factory=deque)  # (t, line)
    active: bool = False
    path: Path | None = None
    started_at: float = 0.0
    bytes: int = 0
    pending: list[str] = field(default_factory=list)
    summary: LogSummary | None = None
    last_unexplained: float | None = None
    history: list[LogSummary] = field(default_factory=list)  # for diagnostics

    def record(self, record: dict) -> None:
        now = self.clock()
        line = json.dumps({"t": _iso(now), **record}, default=str)
        self.ring.append((now, line))
        while self.ring and now - self.ring[0][0] > HISTORY:
            self.ring.popleft()
        if self.active:
            self.pending.append(line)
            self.bytes += len(line) + 1

    def event(self, kind: str, data: dict) -> None:
        """Record an event; "value_lost_unexplained" starts (or extends) the log."""
        self.record({"event": kind, **data})
        if kind != "value_lost_unexplained":
            return
        now = self.clock()
        self.last_unexplained = now
        if not self.active:
            self._start(now)
        self.summary.unexplained_losses += 1

    def due_close(self) -> str | None:
        if not self.active:
            return None
        now = self.clock()
        if now - self.started_at >= MAX_SECONDS or self.bytes >= MAX_BYTES:
            return "size or time limit"
        if self.last_unexplained is not None and now - self.last_unexplained >= QUIET_AFTER:
            return "30 minutes after the last unexplained loss"
        return None

    def close(self, reason: str) -> LogSummary:
        self.record({"event": "log_closed", "reason": reason})
        s = self.summary
        s.ended, s.reason = _iso(self.clock()), reason
        self.active = False
        self.history = (self.history + [s])[-KEEP_FILES:]
        self.summary = None
        self.last_unexplained = None
        return s

    def take_pending(self) -> tuple[Path | None, list[str]]:
        lines, self.pending = self.pending, []
        return self.path, lines

    def _start(self, now: float) -> None:
        stamp = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H-%M-%S")
        self.path = self.directory / f"{PREFIX}{stamp}.jsonl"
        self.active = True
        self.started_at = now
        self.summary = LogSummary(str(self.path), _iso(now))
        self.pending = [line for _, line in self.ring] + [json.dumps({"t": _iso(now), "event": "log_started"})]
        self.bytes = sum(len(line) + 1 for line in self.pending)


def write_lines(path: Path, lines: list[str]) -> None:
    """Append lines (run in an executor)."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def prune(directory: Path, keep: int = KEEP_FILES) -> None:
    """Keep the newest `keep` log files (run in an executor)."""
    if not directory.is_dir():
        return
    files = sorted(directory.glob(f"{PREFIX}*.jsonl"), key=os.path.getmtime)
    for old in files[:-keep]:
        old.unlink(missing_ok=True)


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")
