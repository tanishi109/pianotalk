"""End-to-end tests over real CoreMIDI virtual ports (no piano needed).

The recorder runs as a subprocess; this test process plays the "piano" through
a virtual source and listens as the "piano" through a virtual destination.
"""

import os
import subprocess
import sys
import time

import pytest
import rtmidi

from conftest import write_config
from pianotalk import config, storage
from pianotalk.daemon import pump_runloop
from pianotalk.midiutil import analyze, note_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        pump_runloop(0.05)
    return False


def log_text(home):
    p = home / "logs" / "recorder.log"
    return p.read_text() if p.exists() else ""


@pytest.fixture
def daemon(home):
    write_config(home, silence=0.6)
    proc = subprocess.Popen([sys.executable, "-m", "pianotalk.daemon"], cwd=ROOT,
                            env={**os.environ, "PIANOTALK_HOME": str(home)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: "waiting for input port" in log_text(home)), log_text(home)
    yield proc
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def piano(daemon, home):
    """Virtual source created *after* the daemon started (exercises hot-plug)."""
    out = rtmidi.MidiOut()
    out.open_virtual_port("PianoTalk Test Piano")
    assert wait_for(lambda: "listening on input port" in log_text(home)), log_text(home)
    yield out
    out.close_port()


def play(out, events):
    t0 = time.perf_counter()
    for t, m in events:
        d = t0 + t - time.perf_counter()
        if d > 0:
            time.sleep(d)
        out.send_message(m)


def phrase(start=0.0):
    ev = []
    for i, n in enumerate([60, 64, 67, 72]):
        ev += [(start + i * 0.6, [0x90, n, 70 + i]), (start + i * 0.6 + 0.5, [0x80, n, 0])]
    return sorted(ev, key=lambda e: e[0])


def test_records_take_and_shares_port(piano, home):
    # a second app listening to the same source at the same time (e.g. staff viewer)
    other = rtmidi.MidiIn()
    names = other.get_ports()
    other.open_port(config.find_port(names, "PianoTalk Test Piano"))
    got = []
    other.set_callback(lambda e, _: got.append(e[0]))

    play(piano, [(0, [0xB0, 64, 127])] + phrase(0.1) + [(2.5, [0xB0, 64, 0])])
    assert wait_for(lambda: storage.all_recordings(), 5), log_text(home)
    [p] = storage.all_recordings()
    r = analyze(p)
    assert [note_name(n.pitch) for n in r.notes] == ["C4", "E4", "G4", "C5"]
    assert 2.3 < r.duration < 2.7
    assert [e.down for e in r.pedals] == [True, False]
    assert len(got) == 10  # other app saw everything too
    other.close_port()


def test_pedal_keeps_take_open(piano, home):
    play(piano, [(0, [0xB0, 64, 127])] + phrase(0) + [(3.2, [0xB0, 64, 0])])  # 1.4s pedal-only gap
    assert wait_for(lambda: storage.all_recordings(), 5)
    time.sleep(1.0)
    assert len(storage.all_recordings()) == 1


def test_two_takes_and_short_discarded(piano, home):
    play(piano, phrase(0))
    assert wait_for(lambda: len(storage.all_recordings()) == 1, 5)
    play(piano, [(0, [0x90, 60, 80]), (0.1, [0x80, 60, 0])])  # too short
    assert wait_for(lambda: "discarded short take" in log_text(home), 5)
    play(piano, phrase(0))
    assert wait_for(lambda: len(storage.all_recordings()) == 2, 5)


def test_mcp_play_notes_is_heard_and_not_recorded(piano, home):
    """play_notes -> virtual 'piano' destination; the piano echoes it back to its
    output (worst case), and the recorder must not record the echo."""
    import anyio
    from mcp import Client

    from pianotalk.server import mcp

    synth = rtmidi.MidiIn()
    synth.open_virtual_port("PianoTalk Test Synth")
    received = []

    def echo(e, _):
        received.append(e[0])
        piano.send_message(e[0])

    synth.set_callback(echo)
    pump_runloop(0.3)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            assert tools == {"list_recordings", "get_recording", "play_notes",
                             "keep_recording", "delete_recording"}
            res = await c.call_tool("play_notes", {
                "bpm": 120,
                "notes": [{"note": "C4", "start": 0, "duration": 1, "velocity": 90},
                          {"note": "E4", "start": 1, "duration": 1},
                          {"note": "G4", "start": 2, "duration": 2},
                          {"note": "C3", "start": 2, "duration": 2, "velocity": 50}],
                "pedal": [{"start": 0, "end": 4}]})
            return res.content[0].text

    text = anyio.run(run)
    assert "で再生しました" in text, text
    ons = [m for m in received if m[0] == 0x90 and m[2] > 0]
    assert [m[1] for m in ons] == [60, 64, 67, 48]
    assert [0xB0, 123, 0] in received
    [sug] = list(config.suggestions_dir().glob("*.mid"))
    assert len(analyze(sug).notes) == 4
    time.sleep(1.5)
    assert storage.all_recordings() == [], "echo of playback was recorded"
    assert "muted (playback lock)" in log_text(home)
    synth.close_port()


def test_mcp_read_tools(home):
    import anyio
    from mcp import Client

    from pianotalk.midiutil import write_midi
    import mido
    from pianotalk.server import mcp

    write_config(home, out="no-such-port")
    p = config.recordings_dir() / "2026-11-16" / "20261116-101500.mid"
    ev = [(0.0, mido.Message("note_on", note=57, velocity=64)), (1.0, mido.Message("note_off", note=57)),
          (1.0, mido.Message("note_on", note=69, velocity=100)), (2.5, mido.Message("note_off", note=69)),
          (0.0, mido.Message("note_on", note=72, velocity=50)), (2.5, mido.Message("note_off", note=72))]
    write_midi(ev, p, bpm=60)

    async def run():
        async with Client(mcp) as c:
            out = {}
            for name, args in [("list_recordings", {}), ("get_recording", {"id": "latest"}),
                               ("keep_recording", {"id": "20261116-101500"}),
                               ("list_recordings", {"limit": 5}),
                               ("play_notes", {"notes": [{"note": "C4", "start": 0, "duration": 1}]}),
                               ("play_notes", {"notes": [{"note": "C4", "start": 0, "duration": 1}],
                                               "bpm": 500}),
                               ("delete_recording", {"id": "latest"}),
                               ("get_recording", {"id": "latest"})]:
                r = await c.call_tool(name, args)
                out.setdefault(name, []).append((r.is_error, r.content[0].text))
            return out

    out = anyio.run(run)
    print(out["get_recording"][0][1])
    assert "20261116-101500" in out["list_recordings"][0][1]
    assert out["list_recordings"][0][1].startswith("【注意】録音デーモン")  # no recorder running in this test
    rec_text = out["get_recording"][0][1]
    assert "音域: A3–C5" in rec_text and "BPM: 60" in rec_text and "1:2.00" in rec_text
    assert "[保存印]" in out["list_recordings"][1][1]
    assert out["play_notes"][0][0] is True  # missing port is a tool error, not a success
    assert "見つからないため再生しませんでした" in out["play_notes"][0][1]
    assert "「no-such-port」" in out["play_notes"][0][1] and "suggestions" in out["play_notes"][0][1]
    assert out["play_notes"][1][0] is True and "範囲外" in out["play_notes"][1][1]
    assert "ゴミ箱へ移しました" in out["delete_recording"][0][1]
    assert out["get_recording"][1][0] is True  # no recordings left -> tool error
    assert "録音がまだありません" in out["get_recording"][1][1]
    assert "録音デーモン" in out["get_recording"][1][1]


def test_replay_file_is_heard_stoppable_and_not_recorded(piano, home):
    """Menu bar 聴き返す: a recording is replayed to the 'piano'; the piano's echo
    must not become a new recording, and stop() cuts playback short."""
    import mido

    from pianotalk.midiutil import write_midi
    from pianotalk.player import Player

    src = config.recordings_dir() / "2026-11-16" / "20261116-101500.mid"
    ev = []
    for i, n in enumerate([60, 62, 64, 65, 67]):
        ev += [(i * 0.3, mido.Message("note_on", note=n, velocity=70)),
               (i * 0.3 + 0.25, mido.Message("note_off", note=n))]
    ev += [(0.0, mido.Message("control_change", control=64, value=127)),
           (1.4, mido.Message("control_change", control=64, value=0))]
    write_midi(ev, src, bpm=None)

    synth = rtmidi.MidiIn()
    synth.open_virtual_port("PianoTalk Test Synth")
    received = []

    def echo(e, _):
        received.append(e[0])
        piano.send_message(e[0])

    synth.set_callback(echo)
    pump_runloop(0.3)

    pl = Player(config.load())
    pl.start(src)
    assert pl.active
    assert wait_for(lambda: not pl.active, 5)
    ons = [m for m in received if m[0] & 0xF0 == 0x90 and m[2] > 0]
    assert [m[1] for m in ons] == [60, 62, 64, 65, 67]
    assert [0xB0, 64, 127] in received and [0xB0, 123, 0] in received
    time.sleep(2.0)  # > silence_seconds + post-play mute
    assert storage.all_recordings() == [src], "echo of replay was recorded"

    received.clear()
    pl.start(src)
    time.sleep(0.45)
    pl.stop()
    assert not pl.active
    ons = [m for m in received if m[0] & 0xF0 == 0x90 and m[2] > 0]
    assert 1 <= len(ons) < 5
    assert [0xB0, 123, 0] in received  # stop silences the piano
    synth.close_port()


def test_presence_follows_daemon(daemon, home):
    """What the MCP server tells Claude tracks the real recorder process."""
    from pianotalk import presence
    assert wait_for(lambda: presence.read() is not None)
    assert "接続されていない" in presence.warning()
    out = rtmidi.MidiOut()
    out.open_virtual_port("PianoTalk Test Piano")
    assert wait_for(lambda: presence.warning() is None), presence.read()
    daemon.terminate()
    daemon.wait(5)
    assert presence.read() is None and "動いていない" in presence.warning()
    out.close_port()
