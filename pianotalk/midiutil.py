"""Note names, writing MIDI files, and turning a recording into readable text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import mido

TICKS_PER_BEAT = 480
FILE_BPM_FALLBACK = 120.0  # tempo used in the file when no BPM is configured
BPM_MARKER = "pianotalk:bpm="  # text meta written only when the user configured a BPM

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_STEP = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"^\s*([A-Ga-g])([#♯b♭x]*)(-?\d+)\s*$")

CC_NAMES = {64: "ペダル(サステイン)", 66: "ソステヌート", 67: "ソフトペダル"}


def note_name(n: int) -> str:
    return f"{NAMES[n % 12]}{n // 12 - 1}"


def parse_note(v: str | int) -> int:
    """'C4' -> 60, 'F#3', 'Bb5', 'Ebb4' etc. Integers are passed through."""
    if isinstance(v, int) or (isinstance(v, str) and v.strip().lstrip("-").isdigit()):
        n = int(v)
    else:
        m = _NOTE_RE.match(str(v))
        if not m:
            raise ValueError(f"音名を解釈できません: {v!r}（例: C4, F#3, Bb5）")
        letter, acc, octave = m.groups()
        n = _STEP[letter.upper()] + (int(octave) + 1) * 12
        for ch in acc:
            n += {"#": 1, "♯": 1, "x": 2, "b": -1, "♭": -1}[ch]
    if not 0 <= n <= 127:
        raise ValueError(f"音域外です: {v!r}")
    return n


def write_midi(events: list[tuple[float, mido.Message]], path: Path, bpm: float | None,
               beats_per_bar: int = 4, track_name: str = "PianoTalk") -> None:
    """events: (seconds_from_start, message), any order."""
    file_bpm = bpm or FILE_BPM_FALLBACK
    mid = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage("track_name", name=track_name, time=0))
    tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(file_bpm), time=0))
    if bpm:
        tr.append(mido.MetaMessage("time_signature", numerator=beats_per_bar, denominator=4, time=0))
        tr.append(mido.MetaMessage("text", text=f"{BPM_MARKER}{bpm:g}", time=0))
    ticks_per_sec = file_bpm / 60.0 * TICKS_PER_BEAT
    # stable sort: note_offs before note_ons at the same tick keeps repeated notes sane
    order = sorted(enumerate(events), key=lambda x: (x[1][0], 0 if _is_off(x[1][1]) else 1, x[0]))
    last = 0
    for _, (t, msg) in order:
        tick = max(0, round(t * ticks_per_sec))
        tr.append(msg.copy(time=tick - last))
        last = tick
    tr.append(mido.MetaMessage("end_of_track", time=0))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".mid.tmp")
    mid.save(tmp)
    tmp.replace(path)


def _is_off(m: mido.Message) -> bool:
    return m.type == "note_off" or (m.type == "note_on" and m.velocity == 0)


@dataclass
class Note:
    pitch: int
    start: float
    duration: float
    velocity: int


@dataclass
class PedalEvent:
    time: float
    control: int
    down: bool


@dataclass
class Recording:
    notes: list[Note] = field(default_factory=list)
    pedals: list[PedalEvent] = field(default_factory=list)
    duration: float = 0.0
    bpm: float | None = None  # only when the user configured one
    beats_per_bar: int = 4


def analyze(path: Path) -> Recording:
    mid = mido.MidiFile(path)
    rec = Recording()
    t = 0.0
    open_notes: dict[int, list[tuple[float, int]]] = {}
    pedal_state: dict[int, bool] = {}
    for msg in mid:  # iteration yields delta times in seconds, honoring tempo
        t += msg.time
        if msg.is_meta:
            if msg.type == "text" and msg.text.startswith(BPM_MARKER):
                try:
                    rec.bpm = float(msg.text[len(BPM_MARKER):])
                except ValueError:
                    pass
            elif msg.type == "time_signature":
                rec.beats_per_bar = msg.numerator
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            open_notes.setdefault(msg.note, []).append((t, msg.velocity))
        elif _is_off(msg):
            stack = open_notes.get(msg.note)
            if stack:
                s, v = stack.pop(0)
                rec.notes.append(Note(msg.note, s, t - s, v))
        elif msg.type == "control_change" and msg.control in CC_NAMES:
            down = msg.value >= 64
            if pedal_state.get(msg.control, False) != down:
                pedal_state[msg.control] = down
                rec.pedals.append(PedalEvent(t, msg.control, down))
    for pitch, stack in open_notes.items():  # unterminated notes: close at end
        for s, v in stack:
            rec.notes.append(Note(pitch, s, t - s, v))
    rec.notes.sort(key=lambda n: (n.start, n.pitch))
    rec.duration = t
    return rec


def _pos(sec: float, rec: Recording) -> str:
    if not rec.bpm:
        return ""
    beat = sec * rec.bpm / 60.0
    bar = int(beat // rec.beats_per_bar) + 1
    b = beat - (bar - 1) * rec.beats_per_bar + 1
    return f"{bar}:{b:.2f}"


CHORD_WINDOW = 0.04   # onsets closer than this are shown as one chord row
SPREAD_NOTE = 0.015   # a chord row whose strikes spread this much or more gets their order
MAX_ROWS = 400        # beyond this, a whole-take request returns a section overview


@dataclass
class Onset:
    start: float
    notes: list[Note]


def group_onsets(notes: list[Note], window: float = CHORD_WINDOW) -> list[Onset]:
    """Notes struck (nearly) together become one Onset, lowest pitch first."""
    groups: list[Onset] = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if groups and n.start - groups[-1].start <= window:
            groups[-1].notes.append(n)
        else:
            groups.append(Onset(n.start, [n]))
    for g in groups:
        g.notes.sort(key=lambda n: n.pitch)
    return groups


def _nums(vals: list[float], fmt: str) -> str:
    """'4.50' when all equal after formatting, else '4.50 4.49 3.10'."""
    txt = [format(v, fmt) for v in vals]
    return txt[0] if len(set(txt)) == 1 else " ".join(txt)


def _onset_row(g: Onset, prev: float | None, rec: Recording) -> str:
    gap = "     —" if prev is None else f"{g.start - prev:+6.2f}"
    names = " ".join(note_name(n.pitch) for n in g.notes)
    row = (f"{g.start:7.3f} {gap}  {names}  長さ {_nums([n.duration for n in g.notes], '.2f')}"
           f"  vel {_nums([n.velocity for n in g.notes], 'd')}")
    spread = max(n.start for n in g.notes) - g.start
    if len(g.notes) > 1 and spread >= SPREAD_NOTE - 1e-9:
        order = "→".join(note_name(n.pitch)
                         for n in sorted(g.notes, key=lambda n: (n.start, n.pitch)))
        row += f"  （ずれ {spread * 1000:.0f}ms: {order}）"
    if rec.bpm:
        row += f"  [{_pos(g.start, rec)}]"
    return row


def _pedal_row(p: PedalEvent, rec: Recording) -> str:
    row = f"{p.time:7.3f}        {CC_NAMES[p.control]} {'踏む' if p.down else '離す'}"
    if rec.bpm:
        row += f"  [{_pos(p.time, rec)}]"
    return row


def _section_length(duration: float) -> float:
    """5 s steps, at least 10 s, aiming for at most ~30 sections."""
    step = 10.0
    while duration / step > 30:
        step += 5.0
    return step


def _overview(rec: Recording, onsets: list[Onset]) -> list[str]:
    step = _section_length(rec.duration)
    lines = [f"## 区間ごとの概要（{step:g} 秒ごと）",
             f"音が多いため、各音は省略しています（打鍵 {len(onsets)} 回 > {MAX_ROWS}）。"
             "詳しく見たい区間は get_recording の start_sec / end_sec で指定してください"
             "（目安: 1 回 30–60 秒）。",
             "区間秒        音数  音域       vel平均  ペダル  よく出る音"]
    t = 0.0
    while t < rec.duration or t == 0.0:
        sec = [n for n in rec.notes if t <= n.start < t + step]
        peds = sum(1 for p in rec.pedals if p.control == 64 and p.down and t <= p.time < t + step)
        if sec:
            lo, hi = min(n.pitch for n in sec), max(n.pitch for n in sec)
            vel = sum(n.velocity for n in sec) / len(sec)
            counts: dict[int, int] = {}
            for n in sec:
                counts[n.pitch % 12] = counts.get(n.pitch % 12, 0) + 1
            top = " ".join(NAMES[pc] for pc, _ in sorted(counts.items(), key=lambda x: -x[1])[:4])
            rng = f"{note_name(lo)}–{note_name(hi)}"
            lines.append(f"{t:5.0f}–{min(t + step, rec.duration):<6.0f} {len(sec):5d}  {rng:<9}  "
                         f"{vel:5.0f}  {peds:5d}  {top}")
        else:
            lines.append(f"{t:5.0f}–{min(t + step, rec.duration):<6.0f}     0  （休み）")
        t += step
    return lines


def format_recording(rec: Recording, title: str, start: float | None = None,
                     end: float | None = None) -> str:
    notes = rec.notes
    lines = [f"# 録音 {title}", ""]
    lines.append("## 要約")
    onsets_all = group_onsets(notes)
    if notes:
        lo = min(notes, key=lambda n: n.pitch).pitch
        hi = max(notes, key=lambda n: n.pitch).pitch
        vels = [n.velocity for n in notes]
        lines.append(f"- 音域: {note_name(lo)}–{note_name(hi)}（MIDI {lo}–{hi}）")
        lines.append(f"- 音数: {len(notes)}（打鍵 {len(onsets_all)} 回。"
                     f"{CHORD_WINDOW * 1000:.0f}ms 以内に鳴った音は和音として 1 行にまとめ、"
                     f"打鍵が {SPREAD_NOTE * 1000:.0f}ms 以上ずれた行には「ずれ」と弾いた順を付けています）")
        lines.append(f"- 演奏時間: {rec.duration:.2f} 秒")
        if rec.duration > 0:
            lines.append(f"- 密度: 約 {len(onsets_all) / rec.duration:.1f} 打鍵/秒")
        lines.append(f"- ベロシティ: 平均 {sum(vels) / len(vels):.0f}（{min(vels)}–{max(vels)}）")
    else:
        lines.append("- 音なし")
        lines.append(f"- 演奏時間: {rec.duration:.2f} 秒")
    n_pedal = sum(1 for p in rec.pedals if p.control == 64 and p.down)
    lines.append(f"- サステインペダル: {n_pedal} 回踏み")
    if rec.bpm:
        total_beats = rec.duration * rec.bpm / 60
        lines.append(f"- BPM: {rec.bpm:g}（{rec.beats_per_bar}/4拍子として約 {total_beats / rec.beats_per_bar:.1f} 小節。"
                     "メトロノームに合わせていない演奏ではずれます）")
    else:
        lines.append("- BPM: 未設定（時刻は秒のみ。play_notes で秒のまま再現するなら bpm=60 で 1拍=1秒）")
    lines.append("- 音名はシャープ表記です（A# = B♭ など、調に合わせて読み替えてください）")

    whole = start is None and end is None
    lo_t = start if start is not None else float("-inf")
    hi_t = end if end is not None else float("inf")
    sel = [g for g in onsets_all if lo_t <= g.start <= hi_t]
    sel_ped = [p for p in rec.pedals if lo_t <= p.time <= hi_t]
    lines.append("")
    if whole and len(sel) > MAX_ROWS:
        lines.extend(_overview(rec, onsets_all))
        return "\n".join(lines)

    truncated_at = None
    if len(sel) > MAX_ROWS:
        truncated_at = sel[MAX_ROWS].start
        sel = sel[:MAX_ROWS]
        sel_ped = [p for p in sel_ped if p.time < truncated_at]
    rng = "" if whole else f"（{start or 0:.2f}–{end if end is not None else rec.duration:.2f} 秒の範囲）"
    lines.append(f"## イベント（時刻順）{rng}")
    lines.append("開始秒   間隔  音（低い順）、各音の長さ（秒）とベロシティ" + ("、[小節:拍]" if rec.bpm else ""))
    events: list[tuple[float, int, str]] = []
    prev = None
    for g in sel:
        events.append((g.start, 1, _onset_row(g, prev, rec)))
        prev = g.start
    for p in sel_ped:
        # pedal-up sorts before notes at same instant, pedal-down after
        events.append((p.time, 2 if p.down else 0, _pedal_row(p, rec)))
    events.sort(key=lambda e: (e[0], e[1]))
    lines.extend(e[2] for e in events)
    if truncated_at is not None:
        lines.append(f"（ここまで {MAX_ROWS} 打鍵。続きは start_sec={truncated_at:.2f} で取得してください）")
    return "\n".join(lines)
