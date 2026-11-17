"""Replay a .mid file on the piano from the recorder daemon (menu bar "聴き返す").

Playback runs on its own thread and can be stopped at any time. It holds the
same playback lock as the MCP server, so the recorder ignores the piano while
(and just after) it plays.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import mido
import rtmidi

from . import config, lock

log = logging.getLogger("pianotalk.player")

PLAYED_TYPES = {"note_on", "note_off", "control_change", "program_change", "pitchwheel"}


def load_events(path: Path) -> list[tuple[float, list[int]]]:
    """(seconds from start, MIDI bytes) for every channel message, tempo honored."""
    t = 0.0
    out: list[tuple[float, list[int]]] = []
    for msg in mido.MidiFile(path):
        t += msg.time
        if not msg.is_meta and msg.type in PLAYED_TYPES:
            out.append((t, msg.bytes()))
    return out


class Player:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.path: Path | None = None
        self.started: float = 0.0  # monotonic
        self.total: float = 0.0
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def elapsed(self) -> float:
        return min(self.total, time.monotonic() - self.started) if self.active else 0.0

    def start(self, path: Path) -> None:
        """Begin playing `path` (stops any current replay first). Raises on bad input."""
        self.stop()
        events = load_events(path)
        if not events:
            raise ValueError("音のないファイルです")
        out = rtmidi.MidiOut(rtmidi.API_MACOSX_CORE, name="PianoTalk Replay")
        idx = config.find_port(out.get_ports(), self.cfg.output_port)
        if idx is None:
            del out
            raise LookupError(f"ピアノ（{config.describe_port(self.cfg.output_port)}）が見つかりません")
        out.open_port(idx, "PianoTalk Replay Out")
        self.path, self.total, self.error = path, events[-1][0], None
        self._stop.clear()
        self.started = time.monotonic()
        self._thread = threading.Thread(target=self._run, args=(out, events), daemon=True,
                                        name="pianotalk-replay")
        self._thread.start()
        log.info("replay started: %s (%.1fs)", path, self.total)

    def stop(self, wait: float = 2.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(wait)
        self._thread = None

    def _run(self, out, events) -> None:
        try:
            with lock.playing(self.total, self.cfg.post_play_mute_seconds):
                time.sleep(0.05)  # let the recorder see the lock before the first note
                t0 = time.perf_counter()
                for t, data in events:
                    delay = t0 + t - time.perf_counter()
                    if delay > 0 and self._stop.wait(delay):
                        log.info("replay stopped by user")
                        break
                    out.send_message(data)
                for ch in range(16):
                    out.send_message([0xB0 | ch, 64, 0])   # sustain off
                    out.send_message([0xB0 | ch, 123, 0])  # all notes off
                time.sleep(0.05)
        except Exception as e:
            log.exception("replay failed")
            self.error = str(e)
        finally:
            out.close_port()
            del out
        log.info("replay finished: %s", self.path)
