"""Playback lock shared between the MCP server (writer) and the recorder (reader).

The lock file holds {"pid": ..., "since": epoch, "until": epoch}. The recorder ignores
MIDI input while the lock is active, which covers both our own playback and a
piano that echoes received MIDI back on its output.

- While playing, `until` = expected end + margin, so a crashed player cannot mute
  the recorder for long; a dead pid also releases it immediately.
- After playing, `until` is moved to now + post_play_mute_seconds to swallow
  late echoes, then the file simply expires.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager

from . import config


def _write(since: float, until: float) -> None:
    p = config.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "since": since, "until": until}), encoding="utf-8")
    tmp.replace(p)


@contextmanager
def playing(expected_seconds: float, post_mute: float):
    since = time.time()
    _write(since, since + expected_seconds + 5.0)
    try:
        yield
    finally:
        _write(since, time.time() + post_mute)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class LockWatcher:
    """Cheap repeated check for the recorder (re-reads only when mtime changes)."""

    def __init__(self) -> None:
        self._mtime: float | None = None
        self._data: dict | None = None

    def active(self, at: float | None = None) -> bool:
        """Was playback active at wall-clock time `at` (default: now)?
        The recorder passes each message's arrival time, so processing it a
        little later cannot change the verdict."""
        at = time.time() if at is None else at
        p = config.lock_path()
        try:
            m = p.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self._data = None, None
            return False
        if m != self._mtime:
            self._mtime = m
            try:
                self._data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = None
        d = self._data
        if not d:
            return False
        if not float(d.get("since", 0)) <= at <= float(d.get("until", 0)):
            return False
        return _pid_alive(int(d.get("pid", 0)))

    def holder(self) -> int | None:
        """pid that wrote the lock last time it was read (call after active())."""
        return int(self._data["pid"]) if self._data and "pid" in self._data else None
