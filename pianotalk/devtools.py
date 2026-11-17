"""Tools for trying PianoTalk without a piano (virtual CoreMIDI ports).

  pianotalk-fake-piano   creates a virtual *source* "PianoTalk Fake Piano" and plays
                         a short phrase into it (set input_port = "Fake Piano").
  pianotalk-monitor      creates a virtual *destination* "PianoTalk Monitor" and prints
                         what play_notes sends (set output_port = "PianoTalk Monitor").
"""

from __future__ import annotations

import argparse
import time

import mido
import rtmidi

from .daemon import pump_runloop
from .midiutil import note_name, parse_note

# C major: arpeggio with pedal, then a melody over a held chord
PHRASE = [  # (beat, note, beats, velocity)
    (0, "C3", 4, 60), (0.5, "G3", 3.5, 55), (1, "E4", 1, 72), (2, "G4", 1, 76), (3, "C5", 1, 84),
    (4, "F3", 4, 58), (4.5, "C4", 3.5, 52), (5, "A4", 1, 74), (6, "C5", 1, 78), (7, "F5", 1, 88),
    (8, "G3", 2, 62), (8, "B3", 2, 50), (8, "D4", 2, 50), (8, "G4", 1, 80), (9, "F4", 1, 70),
    (10, "C3", 2, 64), (10, "E4", 2, 60), (10, "C5", 2, 82),
]
PEDAL = [(0, 3.9), (4, 7.9), (10, 12)]


def fake_piano() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bpm", type=float, default=100)
    ap.add_argument("--repeat", type=int, default=1, help="繰り返し回数（間に --gap 秒の休み）")
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--wait", type=float, default=3.0, help="ポート作成後、演奏開始までの秒数")
    a = ap.parse_args()
    out = rtmidi.MidiOut()
    out.open_virtual_port("PianoTalk Fake Piano")
    print("仮想入力ポート 'PianoTalk Fake Piano' を作成しました。", flush=True)
    pump_runloop(a.wait)
    spb = 60 / a.bpm
    ev = []
    for b, n, d, v in PHRASE:
        p = parse_note(n)
        ev += [(b * spb, [0x90, p, v]), ((b + d) * spb, [0x80, p, 0])]
    for s, e in PEDAL:
        ev += [(s * spb, [0xB0, 64, 127]), (e * spb, [0xB0, 64, 0])]
    ev.sort(key=lambda x: (x[0], 0 if x[1][0] in (0x80,) or x[1][2] == 0 else 1))
    for r in range(a.repeat):
        print(f"演奏 {r + 1}/{a.repeat} …", flush=True)
        t0 = time.perf_counter()
        for t, m in ev:
            d = t0 + t - time.perf_counter()
            if d > 0:
                time.sleep(d)
            out.send_message(m)
        if r + 1 < a.repeat:
            time.sleep(a.gap)
    print("演奏終了。録音デーモンが無音を検出すると保存されます。ポートを閉じるまで少し待ちます…", flush=True)
    time.sleep(1.0)
    out.close_port()


def monitor() -> None:
    inp = rtmidi.MidiIn()
    inp.open_virtual_port("PianoTalk Monitor")
    print("仮想出力ポート 'PianoTalk Monitor' を作成しました。Ctrl-C で終了。", flush=True)
    t0 = None

    def cb(event, _):
        nonlocal t0
        now = time.monotonic()
        t0 = t0 if t0 is not None else now
        msg = mido.Message.from_bytes(event[0])
        desc = str(msg)
        if msg.type in ("note_on", "note_off"):
            desc = f"{msg.type:<8} {note_name(msg.note):<4} vel={msg.velocity}"
        print(f"{now - t0:8.3f}  {desc}", flush=True)
        if msg.type == "control_change" and msg.control == 123:
            t0 = None

    inp.set_callback(cb)
    try:
        while True:
            pump_runloop(0.2)
    except KeyboardInterrupt:
        pass
