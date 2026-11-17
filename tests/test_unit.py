import os
from datetime import datetime, timedelta

import mido
import pytest

from conftest import write_config
from pianotalk import config, lock, storage
from pianotalk.daemon import Recorder
from pianotalk.midiutil import analyze, format_recording, note_name, parse_note, write_midi


def test_note_names():
    assert note_name(60) == "C4"
    assert note_name(21) == "A0"
    assert note_name(61) == "C#4"
    assert parse_note("C4") == 60
    assert parse_note("Bb3") == 58
    assert parse_note("f#5") == 78
    assert parse_note(64) == 64
    with pytest.raises(ValueError):
        parse_note("H2")


def on(n, v=80):
    return mido.Message("note_on", note=n, velocity=v)


def off(n):
    return mido.Message("note_off", note=n, velocity=0)


def cc(v):
    return mido.Message("control_change", control=64, value=v)


def test_roundtrip_with_bpm(home):
    p = home / "x.mid"
    ev = [(0.0, cc(127)), (0.0, on(60, 70)), (0.5, off(60)), (0.5, on(64)), (1.0, off(64)),
          (1.5, cc(0)), (1.0, on(67, 90)), (2.25, off(67))]
    write_midi(ev, p, bpm=120, beats_per_bar=3)
    r = analyze(p)
    assert r.bpm == 120 and r.beats_per_bar == 3
    assert [(note_name(n.pitch), round(n.start, 3), round(n.duration, 3), n.velocity) for n in r.notes] == [
        ("C4", 0.0, 0.5, 70), ("E4", 0.5, 0.5, 80), ("G4", 1.0, 1.25, 90)]
    assert [(round(e.time, 3), e.down) for e in r.pedals] == [(0.0, True), (1.5, False)]
    text = format_recording(r, "test")
    assert "音域: C4–G4" in text and "音数: 3" in text
    assert "[1:3.00]" in text  # G4 at 1.0s = beat 2 -> bar 1, beat 3 in 3/4
    assert "ペダル(サステイン) 踏む" in text


def test_no_bpm_has_no_bar_positions(home):
    p = home / "y.mid"
    write_midi([(0.0, on(60)), (0.3, off(60))], p, bpm=None)
    r = analyze(p)
    assert r.bpm is None
    assert abs(r.notes[0].duration - 0.3) < 0.01
    assert "小節:拍" not in format_recording(r, "t")


def test_recorder_state_machine(home):
    write_config(home, silence=3.0)
    rec = Recorder(config.load())
    rec.handle(cc(127), 9.0)  # pedal before first note: not recording yet
    assert not rec.recording
    rec.handle(on(60), 10.0)
    assert rec.recording
    rec.handle(off(60), 10.5)
    rec.handle(on(62), 11.0)
    rec.handle(off(62), 11.5)
    rec.tick(20.0)  # pedal still down -> keeps recording
    assert rec.recording
    rec.handle(on(64), 21.0)
    rec.handle(off(64), 21.5)
    rec.handle(cc(0), 22.0)
    rec.tick(24.9)
    assert rec.recording
    rec.tick(25.0)
    assert not rec.recording
    [p] = storage.all_recordings()
    r = analyze(p)
    assert len(r.notes) == 3
    assert abs(r.duration - 12.0) < 0.01  # ends at pedal release, no trailing silence
    assert r.pedals[0].down and r.pedals[0].time == 0.0  # pre-pressed pedal recorded at t=0
    assert p.parent.name == datetime.now().strftime("%Y-%m-%d")


@pytest.mark.parametrize("events", [
    [(0, on(60)), (3, off(60)), (3, on(61)), (3.1, off(61))],  # 2 notes
    [(0, on(60)), (0.1, on(61)), (0.2, on(62)), (1.0, off(60)), (1.0, off(61)), (1.0, off(62))],  # 1s
])
def test_short_takes_discarded(home, events):
    write_config(home, silence=1.0)
    rec = Recorder(config.load())
    for t, m in events:
        rec.handle(m, 100 + t)
    rec.tick(200)
    assert storage.all_recordings() == []


def _mk(home, stem, kept=False):
    d = config.kept_dir() if kept else config.recordings_dir() / f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}"
    p = d / f"{stem}.mid"
    write_midi([(0.0, on(60)), (1.0, off(60))], p, None)
    return p


def test_resolve_keep_delete_cleanup(home, trash_dir):
    old = _mk(home, "20250101-100000")
    old_kept = _mk(home, "20250102-100000", kept=True)
    new = _mk(home, "20261115-090000")
    assert storage.resolve("latest") == new
    assert storage.resolve("20250101-100000.mid") == old
    with pytest.raises(FileNotFoundError):
        storage.resolve("../../etc/passwd")
    removed = storage.cleanup(30, now=datetime(2026, 11, 16))
    assert removed == [old]
    assert not old.exists() and not old.parent.exists()  # empty day folder removed
    assert (trash_dir / old.name).exists()
    assert old_kept.exists() and new.exists()
    k = storage.keep("latest")
    assert k.parent == config.kept_dir() and storage.is_kept(k)
    assert storage.cleanup(1, now=datetime(2030, 1, 1)) == []
    storage.delete(k.stem)
    assert not k.exists() and (trash_dir / k.name).exists()


def test_lock(home):
    w = lock.LockWatcher()
    assert not w.active()
    with lock.playing(1.0, post_mute=0.3):
        assert w.active()
    assert w.active()  # post-play mute
    import time
    time.sleep(0.35)
    assert not w.active()
    # stale lock from a dead process is ignored
    config.lock_path().write_text('{"pid": 999999, "until": %f}' % (time.time() + 100))
    assert not lock.LockWatcher().active()


def test_state_tracked_while_not_recording(home):
    """Key released while muted/paused must not leave the take open forever."""
    write_config(home, silence=1.0)
    rec = Recorder(config.load())
    for i, n in enumerate([60, 62, 64]):
        rec.handle(on(n), 10 + i)
        rec.handle(off(n), 10.5 + i)
    rec.handle(on(67), 13.0)
    rec.handle(off(67), 13.4, record=False)  # released during playback lock / pause
    assert rec.held == set()
    rec.tick(14.5)
    assert not rec.recording
    [p] = storage.all_recordings()
    r = analyze(p)
    assert len(r.notes) == 4  # G4 closed at the last recorded event
    assert rec.last_saved[0] == p and rec.last_saved[1] == 4


def test_unrecorded_input_never_starts_take(home):
    write_config(home)
    rec = Recorder(config.load())
    rec.handle(on(60), 1.0, record=False)
    rec.handle(mido.Message("control_change", control=123, value=0), 1.1, record=False)
    assert not rec.recording and rec.held == set()


def test_lock_judged_by_arrival_time(home):
    import time
    before = time.time()
    time.sleep(0.01)
    w = lock.LockWatcher()
    with lock.playing(1.0, post_mute=0.2):
        during = time.time()
        # a note played just *before* playback started, processed late, is kept
        assert not w.active(at=before)
        assert w.active(at=during)
    after_end = time.time()
    time.sleep(0.3)
    # an echo that arrived during the post-play mute is still muted even if
    # it is processed after the lock has expired
    assert w.active(at=after_end)
    assert not w.active()


def test_loop_interval_fast_only_while_recording(home):
    from pianotalk import daemon as d
    write_config(home)
    dm = d.Daemon()
    assert dm.interval() == d.IDLE_INTERVAL
    dm.rec.handle(on(60), 1.0)
    assert dm.interval() == d.ACTIVE_INTERVAL


def test_chords_grouped_into_one_row(home):
    from pianotalk.midiutil import group_onsets
    p = home / "c.mid"
    ev = [(0.0, on(60, 40)), (0.008, on(64, 60)), (0.012, on(67, 60)),
          (1.0, off(60)), (1.0, off(64)), (1.0, off(67)),
          (1.0, on(62)), (1.5, off(62))]
    write_midi(ev, p, bpm=None)
    r = analyze(p)
    assert [len(g.notes) for g in group_onsets(r.notes)] == [3, 1]
    text = format_recording(r, "t")
    rows = [l for l in text.splitlines() if "長さ" in l and "vel" in l]
    assert len(rows) == 2
    assert "C4 E4 G4" in rows[0] and "vel 40 60 60" in rows[0]
    assert "+1.00" in rows[1] and "D4" in rows[1]
    assert "打鍵 2 回" in text and "bpm=60" in text
    assert "ずれ" not in rows[0]  # 12 ms spread: a plain chord


def test_rolled_chord_and_grace_note_show_spread_and_order(home):
    p = home / "roll.mid"
    ev = [(0.0, on(67)), (0.016, on(64)), (0.032, on(60)),   # downward roll
          (1.0, off(60)), (1.0, off(64)), (1.0, off(67)),
          (2.0, on(61)), (2.025, on(62)),                     # grace note C#4 -> D4
          (2.1, off(61)), (2.5, off(62))]
    write_midi(ev, p, bpm=None)
    rows = [l for l in format_recording(analyze(p), "t").splitlines() if "長さ" in l and "vel" in l]
    assert len(rows) == 2
    assert "C4 E4 G4" in rows[0] and "（ずれ 32ms: G4→E4→C4）" in rows[0]
    assert "C#4 D4" in rows[1] and "（ずれ 25ms: C#4→D4）" in rows[1]


def test_long_take_gets_overview_and_range_is_detailed(home):
    from pianotalk import midiutil
    p = home / "long.mid"
    ev = []
    for i in range(500):  # 500 separate strikes, 0.25 s apart (125 s)
        n = 60 + (i % 12)
        ev += [(i * 0.25, on(n)), (i * 0.25 + 0.2, off(n))]
    write_midi(ev, p, bpm=None)
    r = analyze(p)
    text = format_recording(r, "t")
    assert "区間ごとの概要" in text and "start_sec" in text
    assert len(text) < 4000
    detail = format_recording(r, "t", start=10, end=20)
    assert "区間ごとの概要" not in detail
    assert len([l for l in detail.splitlines() if "長さ" in l and "vel" in l]) == 41
    full = format_recording(r, "t", start=0)  # explicit range: truncated with a pointer
    assert f"続きは start_sec={midiutil.MAX_ROWS * 0.25:.2f}" in full


def test_take_outcome_and_save_countdown(home):
    from pianotalk import daemon as d
    write_config(home, silence=3.0)
    dm = d.Daemon()
    dm.rec.handle(on(60), 1.0)
    dm.rec.handle(off(60), 1.2)
    st = dm.status()
    assert st["recording"] and st["until_save"] is not None and not st["will_save"]
    dm.rec.finish()
    assert dm.rec.last_result["kind"] == "discarded" and dm.rec.last_result["notes"] == 1
    for i, n in enumerate([60, 62, 64]):
        dm.rec.handle(on(n), 10 + i)
        if i < 2:
            dm.rec.handle(off(n), 10.5 + i)
    st = dm.status()
    assert st["until_save"] is None  # E4 still held
    dm.rec.handle(off(64), 12.5)
    assert dm.status()["will_save"]
    dm.rec.finish()
    assert dm.rec.last_result["kind"] == "saved" and dm.rec.last_result["notes"] == 3


def test_replay_refused_while_recording_or_unplugged(home):
    from pianotalk import daemon as d
    write_config(home, out="no-such-port")
    p = _mk(home, "20261116-101500")
    dm = d.Daemon()
    assert "見つかりません" in dm.replay(p)
    dm.rec.handle(on(60), 1.0)
    assert "録音中" in dm.replay(p)
    assert not dm.player.active
    assert [s.stem for s in storage.all_suggestions()] == []


def test_find_port_by_name_and_auto():
    names = ["IAC Driver Bus 1", "Network Session 1", "PianoTalk Fake Piano", "CN201 Bluetooth"]
    assert config.find_port(names, "cn201") == 3
    assert config.find_port(names, "nothing") is None
    assert config.find_port(names, "") == 3        # only one real device
    assert config.find_port(names, "  ") == 3
    assert config.find_port(names + ["Digital Piano"], "") is None  # ambiguous
    assert config.find_port(names[:3], "") is None  # no device
    assert config.find_port([None, "CN201 Bluetooth"], "cn201") == 1  # unnamed port
    assert config.find_port([None, "CN201 Bluetooth"], "") == 1
    assert config.describe_port("") == "MIDI機器（自動選択）"
    assert config.describe_port("P-125") == "「P-125」"


def test_default_config_has_no_device_name(home):
    config.ensure_default_config()
    c = config.load()
    assert c.input_port == "" and c.output_port == ""


def test_presence_warning_states(home):
    import json, os, subprocess, sys
    from pianotalk import presence

    assert "動いていない" in presence.warning()  # never started
    presence.write({"port": "CN201 Bluetooth", "paused": False, "wanted": "「CN201」", "ambiguous": 0})
    assert presence.warning() is None
    assert "応答していません" in presence.warning(now=presence.read()["updated"] + presence.STALE + 5)

    presence.write({"port": "CN201 Bluetooth", "paused": True, "wanted": "「CN201」", "ambiguous": 0})
    assert "一時停止中" in presence.warning()
    presence.write({"port": None, "paused": False, "wanted": "MIDI機器（自動選択）", "ambiguous": 0})
    assert "MIDI機器（自動選択）" in presence.warning() and "接続されていない" in presence.warning()
    presence.write({"port": None, "paused": False, "wanted": "MIDI機器（自動選択）", "ambiguous": 2})
    assert "複数" in presence.warning()

    # a crashed recorder leaves the file behind with a dead pid; pid 0/missing never counts as alive
    p = config.recorder_status_path()
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    for pid in (dead.pid, 0, None):
        d = presence.read(); d["pid"] = pid
        p.write_text(json.dumps(d))
        assert "動いていない" in presence.warning()

    # clear() leaves another process's file alone, removes our own
    presence.clear()
    assert p.exists()
    presence.write({"port": "x", "paused": False, "wanted": "", "ambiguous": 0})
    presence.clear()
    assert not p.exists()


def test_daemon_heartbeat_and_clean_exit(home):
    from pianotalk import daemon as d, presence
    write_config(home, inp="no-such-port", out="no-such-port")
    dm = d.Daemon()
    dm.beat(100.0)
    s = presence.read()
    assert s["pid"] == os.getpid() and s["port"] is None and s["paused"] is False
    first = s["updated"]
    dm.beat(101.0)  # nothing changed, heartbeat not due: no rewrite
    assert presence.read()["updated"] == first
    dm.paused = True
    dm.beat(102.0)  # state changed: rewritten at once
    assert presence.read()["paused"] is True
    dm.shutdown()
    assert presence.read() is None
