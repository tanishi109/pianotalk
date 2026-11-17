"""Recorder daemon: auto-records the piano's MIDI output to ~/PianoTalk/recordings.

- CoreMIDI inputs are shared, so another app (e.g. a staff viewer) can read the
  same port at the same time.
- Recording starts on the first note-on and ends once every key and the sustain
  pedal have been released for `silence_seconds`.
- Input is ignored while the MCP server's playback lock is active.
- The port is re-scanned every few seconds, so the piano can be switched on/off
  or the Bluetooth link can drop and come back.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import mido
import rtmidi

from . import config, lock, presence, storage
from .midiutil import write_midi
from .player import Player

log = logging.getLogger("pianotalk.recorder")

PORT_SCAN_INTERVAL = 2.0
# How often the main loop wakes up. MIDI messages are timestamped on arrival, so
# a slower loop delays only when they are processed, never what is recorded.
ACTIVE_INTERVAL = 0.05  # while a take is being recorded (silence detection)
IDLE_INTERVAL = 0.5     # waiting / paused / piano not connected
CLEANUP_INTERVAL = 24 * 3600
RECORDED_CCS = {64, 66, 67}


def setup_logging() -> None:
    config.logs_dir().mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        config.logs_dir() / "recorder.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.INFO)


def prevent_app_nap():
    """Opt out of App Nap / timer coalescing. Returns a token that must stay referenced."""
    try:
        from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSProcessInfo

        # keeps timers/MIDI responsive but still lets the Mac sleep when idle
        token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "PianoTalk recorder listens for MIDI input")
        log.info("App Nap disabled")
        return token
    except Exception as e:
        log.warning("could not disable App Nap: %s", e)
        return None


def pump_runloop(seconds: float) -> None:
    """Run the CFRunLoop briefly so CoreMIDI setup-change notifications are delivered
    (otherwise a long-lived process may not notice ports that appear later)."""
    try:
        from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

        CFRunLoopRunInMode(kCFRunLoopDefaultMode, seconds, False)
    except Exception:
        time.sleep(seconds)


class Recorder:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.events: list[tuple[float, mido.Message]] = []
        self.t0: float | None = None  # monotonic time of first note-on
        self.started_at: datetime | None = None
        self.held: set[int] = set()
        self.sustain = False
        self.quiet_since: float | None = None
        self.last_event: float = 0.0
        self.n_notes = 0
        self.last_saved: tuple[Path, int, float] | None = None  # (path, notes, seconds)
        # outcome of the most recent take, for the menu bar:
        # {"kind": "saved"|"discarded", "notes", "seconds", "at": monotonic, "path"?}
        self.last_result: dict | None = None

    @property
    def recording(self) -> bool:
        return self.t0 is not None

    def handle(self, msg: mido.Message, now: float, record: bool = True) -> None:
        """Track key/pedal state always; add to the take only when `record` is True
        (False while paused or during playback, so state stays correct)."""
        is_on = msg.type == "note_on" and msg.velocity > 0
        is_off = msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)
        is_cc = msg.type == "control_change" and msg.control in RECORDED_CCS
        if msg.type == "control_change" and msg.control in (120, 123):  # all sound/notes off
            self.held.clear()
        if is_cc and msg.control == 64:
            self.sustain = msg.value >= 64
        if is_on:
            self.held.add(msg.note)
        elif is_off:
            self.held.discard(msg.note)
        quiet = not self.held and not self.sustain
        if not (is_on or is_off or is_cc):
            if quiet and self.recording and self.quiet_since is None:
                self.quiet_since = now
            return
        if not record:
            if self.recording:
                self.quiet_since = (self.quiet_since or now) if quiet else None
            return

        if not self.recording:
            if not is_on:
                return
            self.t0 = now
            self.started_at = datetime.now()
            self.events = []
            self.n_notes = 0
            if self.sustain:  # pedal already down before the first note
                self.events.append((0.0, mido.Message("control_change", control=64, value=127)))
            log.info("recording started")
        self.events.append((now - self.t0, msg))
        if is_on:
            self.n_notes += 1
        self.last_event = now
        self.quiet_since = now if quiet else None

    def tick(self, now: float) -> None:
        if self.recording and self.quiet_since is not None \
                and now - self.quiet_since >= self.cfg.silence_seconds:
            self.finish()

    def finish(self) -> Path | None:
        if not self.recording:
            return None
        events, started_at, t0 = self.events, self.started_at, self.t0
        end = self.last_event - t0
        self.t0, self.started_at, self.events, self.quiet_since = None, None, [], None
        # close anything still open (e.g. note-offs swallowed during a playback lock)
        open_notes: dict[int, int] = {}
        for _, m in events:
            if m.type == "note_on" and m.velocity > 0:
                open_notes[m.note] = open_notes.get(m.note, 0) + 1
            elif m.type in ("note_on", "note_off") and open_notes.get(m.note):
                open_notes[m.note] -= 1
        for note, cnt in open_notes.items():
            for _ in range(cnt):
                events.append((end, mido.Message("note_off", note=note, velocity=0)))
        n_notes = sum(1 for _, m in events if m.type == "note_on" and m.velocity > 0)
        if n_notes < self.cfg.min_notes or end < self.cfg.min_duration_seconds:
            log.info("discarded short take (%d notes, %.2fs)", n_notes, end)
            self.last_result = {"kind": "discarded", "notes": n_notes, "seconds": end,
                                "at": time.monotonic()}
            return None
        path = storage.new_recording_path(started_at)
        write_midi(events, path, self.cfg.bpm, self.cfg.beats_per_bar, track_name="Piano")
        log.info("saved %s (%d notes, %.2fs)", path, n_notes, end)
        self.last_saved = (path, n_notes, end)
        self.last_result = {"kind": "saved", "notes": n_notes, "seconds": end,
                            "at": time.monotonic(), "path": path}
        return path

    def seconds_until_save(self, now: float) -> float | None:
        """While every key and the pedal are up, how long until the take is closed."""
        if not self.recording or self.quiet_since is None:
            return None
        return max(0.0, self.cfg.silence_seconds - (now - self.quiet_since))


class Daemon:
    def __init__(self) -> None:
        config.ensure_default_config()
        self.cfg = config.load()
        self.rec = Recorder(self.cfg)
        self.q: queue.SimpleQueue[tuple[float, float, list[int]]] = queue.SimpleQueue()
        self.midi_in = rtmidi.MidiIn(rtmidi.API_MACOSX_CORE, name="PianoTalk Recorder")
        self.midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        self.port_name: str | None = None
        self.ambiguous: list[str] = []  # auto mode found several devices
        self.lock = lock.LockWatcher()
        self.player = Player(self.cfg)
        self.running = True
        self.muted = False  # playback lock active
        self.paused = False  # user paused recording from the menu bar
        self._next_scan = 0.0
        self._next_cleanup = 0.0
        self._waiting_logged = False
        self._presence: tuple | None = None  # last state written to recorder.json
        self._next_beat = 0.0

    def _callback(self, event, _data=None) -> None:
        # runs on CoreMIDI's thread: timestamp immediately, process on main thread
        self.q.put((time.monotonic(), time.time(), event[0]))

    def scan_port(self) -> None:
        try:
            names = self.midi_in.get_ports()
        except Exception:
            log.exception("port list failed")
            return
        if self.port_name is not None and self.port_name in names:
            return
        if self.port_name is not None:
            log.warning("input port disappeared: %s", self.port_name)
            self.close_port()
        idx = config.find_port(names, self.cfg.input_port)
        auto = not self.cfg.input_port.strip()
        ambiguous = [names[i] for i in config.auto_candidates(names)] if auto and idx is None else []
        if len(ambiguous) > 1 and ambiguous != self.ambiguous:
            log.warning("several MIDI devices %s; set input_port in config.toml", ambiguous)
        self.ambiguous = ambiguous if len(ambiguous) > 1 else []
        if idx is None:
            return
        try:
            self.midi_in.open_port(idx, "PianoTalk Recorder In")
            self.midi_in.set_callback(self._callback)
            self.port_name = names[idx]
            log.info("listening on input port: %s", self.port_name)
        except Exception:
            log.exception("failed to open %s", names[idx])
            self.close_port()

    def close_port(self) -> None:
        try:
            self.midi_in.cancel_callback()
        except Exception:
            pass
        try:
            self.midi_in.close_port()
        except Exception:
            pass
        self.port_name = None
        # keys/pedal state is unknown after a disconnect
        self.rec.held.clear()
        self.rec.sustain = False
        if self.rec.recording:
            self.rec.quiet_since = self.rec.last_event

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        if paused:
            self.rec.finish()  # keep what was played so far
        log.info("recording %s by user", "paused" if paused else "resumed")

    def replay(self, path: Path) -> str | None:
        """Play a .mid file on the piano. Returns a reason when it cannot."""
        if self.rec.recording:
            return "録音中は再生できません（録音が保存されてからもう一度どうぞ）"
        if self.claude_playing():
            return "Claude が演奏中です"
        try:
            self.player.start(path)
        except Exception as e:
            log.warning("replay of %s refused: %s", path, e)
            return str(e)
        return None

    def claude_playing(self) -> bool:
        """Playback lock held by the MCP server (not by our own replay)."""
        return self.lock.active() and self.lock.holder() != os.getpid()

    def stop_replay(self) -> None:
        self.player.stop()

    def drain(self) -> None:
        while True:
            try:
                t, wall, data = self.q.get_nowait()
            except queue.Empty:
                return
            muted = self.lock.active(at=wall)
            if muted != self.muted:
                self.muted = muted
                log.info("input %s (playback lock)", "muted" if muted else "unmuted")
            try:
                msg = mido.Message.from_bytes(data)
            except ValueError:
                continue
            self.rec.handle(msg, t, record=not (muted or self.paused))

    def status(self) -> dict:
        """Snapshot for the menu bar."""
        if self.lock.active() != self.muted:
            self.muted = not self.muted
            log.info("input %s (playback lock)", "muted" if self.muted else "unmuted")
        now = time.monotonic()
        until_save = self.rec.seconds_until_save(now)
        if not self.rec.recording:
            elapsed = 0
        elif until_save is not None:  # stop the clock at the last note/pedal event
            elapsed = int(self.rec.last_event - self.rec.t0)
        else:
            elapsed = int(now - self.rec.t0)
        return {
            "port": self.port_name,
            "wanted": config.describe_port(self.cfg.input_port),
            "ambiguous": len(self.ambiguous),
            "recording": self.rec.recording,
            "elapsed": elapsed,
            # whole seconds left before saving, or None while keys/pedal are held
            "until_save": None if until_save is None else int(until_save + 0.999),
            "will_save": self.rec.recording and self.rec.n_notes >= self.cfg.min_notes
                         and self.rec.last_event - self.rec.t0 >= self.cfg.min_duration_seconds,
            "notes": self.rec.n_notes if self.rec.recording else 0,
            "playing": self.muted,
            "replay_tail": self.muted and self.lock.holder() == os.getpid() and not self.player.active,
            "paused": self.paused,
            "last_saved": self.rec.last_saved,
            "last_result": self.rec.last_result,
            "replay": ({"path": self.player.path, "elapsed": int(self.player.elapsed()),
                        "total": int(self.player.total + 0.999)} if self.player.active else None),
            "min_notes": self.cfg.min_notes,
            "min_seconds": self.cfg.min_duration_seconds,
        }

    def start(self) -> None:
        log.info("recorder starting; home=%s input_port~=%r silence=%.1fs bpm=%s",
                 config.home(), self.cfg.input_port, self.cfg.silence_seconds, self.cfg.bpm)
        self._activity = prevent_app_nap()
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self.cleanup()
        recent = storage.all_recordings()
        if recent:
            self.rec.last_saved = (recent[0], 0, 0.0)
        self._next_scan = 0.0
        self._next_cleanup = time.monotonic() + CLEANUP_INTERVAL

    def interval(self) -> float:
        return ACTIVE_INTERVAL if self.rec.recording else IDLE_INTERVAL

    def step(self) -> None:
        """One iteration of work; called every ~50 ms (from our loop or the menu bar timer)."""
        self.drain()
        now = time.monotonic()
        self.rec.tick(now)
        if now >= self._next_scan:
            self.scan_port()
            if self.port_name is None and not self._waiting_logged:
                log.info("waiting for input port %s", config.describe_port(self.cfg.input_port))
            self._waiting_logged = self.port_name is None
            self._next_scan = now + PORT_SCAN_INTERVAL
        if now >= self._next_cleanup:
            self.cleanup()
            self._next_cleanup = now + CLEANUP_INTERVAL
        self.beat(now)

    def beat(self, now: float) -> None:
        """Keep recorder.json fresh: on every state change and every HEARTBEAT seconds."""
        state = (self.port_name, self.paused, len(self.ambiguous))
        if state == self._presence and now < self._next_beat:
            return
        try:
            presence.write({"port": self.port_name, "paused": self.paused,
                            "wanted": config.describe_port(self.cfg.input_port),
                            "ambiguous": len(self.ambiguous)})
        except OSError:
            log.exception("could not write %s", config.recorder_status_path())
        self._presence = state
        self._next_beat = now + presence.HEARTBEAT

    def shutdown(self) -> None:
        self.player.stop()
        self.drain()
        self.rec.finish()
        self.close_port()
        presence.clear()
        log.info("recorder stopped")

    def run(self, menubar: bool | None = None) -> None:
        self.start()
        ui = None
        if self.cfg.menubar if menubar is None else menubar:
            try:
                from .menubar import MenuBar

                ui = MenuBar(self)
            except Exception:
                log.exception("menu bar unavailable; running without it")
        if ui is not None:
            ui.run()  # returns once self.running is False
        else:
            while self.running:
                pump_runloop(self.interval())
                self.step()
        self.shutdown()

    def cleanup(self) -> None:
        try:
            removed = storage.cleanup(self.cfg.retention_days)
            if removed:
                log.info("moved %d expired recording(s) to Trash", len(removed))
        except Exception:
            log.exception("cleanup failed")

    def _stop(self, *_):
        self.running = False


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="PianoTalk recorder daemon")
    ap.add_argument("--no-menubar", action="store_true", help="メニューバーのアイコンを出さない")
    a = ap.parse_args()
    setup_logging()
    Daemon().run(menubar=False if a.no_menubar else None)


def list_ports() -> None:
    """`pianotalk-ports`: print MIDI port names as this program sees them."""
    pump_runloop(0.3)
    print("入力ポート（input_port に指定）:")
    for n in rtmidi.MidiIn().get_ports():
        print(f"  {n}")
    print("出力ポート（output_port に指定）:")
    for n in rtmidi.MidiOut().get_ports():
        print(f"  {n}")


if __name__ == "__main__":
    main()
