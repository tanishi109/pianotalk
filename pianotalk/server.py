"""PianoTalk MCP server (stdio): read recordings and play phrases on the piano."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import functools
import inspect

import anyio
import mido
import rtmidi
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import config, lock, storage
from .midiutil import analyze, format_recording, note_name, parse_note, write_midi

log = logging.getLogger("pianotalk.mcp")

INSTRUCTIONS = """\
ユーザーの電子ピアノ演奏は自動で録音されています（演奏が止まって数秒後に1テイクとして保存）。
- 演奏について話すときは list_recordings / get_recording(id="latest") で録音を読んでください。
- 音名は C4 = 中央のド（MIDI 60）。シャープは #、フラットは b（例: F#3, Bb4）。
  録音の音名は常にシャープ表記なので、調に合わせて読み替えてください（A# = B♭）。
- get_recording はほぼ同時に鳴った音を和音として1行にまとめます。音が多い録音は区間ごとの概要になるので、
  気になる区間を start_sec / end_sec で読み直してください。
- play_notes でフレーズをピアノから鳴らせます。開始と長さは拍単位です。
  録音（秒）のフレーズをそのまま再現・変形するときは bpm=60 にすると 1拍 = 1秒 で秒数をそのまま使えます。
  1回の再生の上限は約120秒です（設定 playback.max_seconds）。
- 再生中と再生後約1秒はユーザーの演奏を録音しません。鳴らす前に「今から弾きます」と一言伝え、
  ユーザーに弾いてもらいたいときは再生が終わってからにしてください。
- 録音は一定期間で自動削除されます。残したいと言われたら keep_recording を使ってください。
"""

mcp = MCPServer("pianotalk", instructions=INSTRUCTIONS)
EXPECTED = (FileNotFoundError, FileExistsError, ValueError)


def tool(fn):
    """mcp.tool(), but anticipated errors reach Claude with their message
    (the SDK hides the text of unexpected exceptions)."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            try:
                return await fn(*a, **kw)
            except EXPECTED as e:
                raise ToolError(str(e)) from e
    else:
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except EXPECTED as e:
                raise ToolError(str(e)) from e
    return mcp.tool()(wrapper)


def _summary_line(p) -> str:
    try:
        rec = analyze(p)
        info = f"{rec.duration:6.1f}秒  {len(rec.notes):4d}音"
        if rec.notes:
            lo = min(n.pitch for n in rec.notes)
            hi = max(n.pitch for n in rec.notes)
            info += f"  {note_name(lo)}–{note_name(hi)}"
    except Exception as e:
        info = f"(読み込み失敗: {e})"
    kept = "  [保存印]" if storage.is_kept(p) else ""
    when = storage.recorded_at(p).strftime("%Y-%m-%d %H:%M:%S")
    return f"{p.stem}  {when}  {info}{kept}"


@tool
def list_recordings(limit: int = 20) -> str:
    """録音を新しい順に一覧します（ID=ファイル名、日時、長さ、音数、音域、保存印）。"""
    files = storage.all_recordings()
    if not files:
        return "録音はまだありません。"
    cfg = config.load()
    lines = [f"録音 {len(files)} 件中 新しい {min(limit, len(files))} 件"
             f"（保存期間 {cfg.retention_days} 日、[保存印] は自動削除されません）:"]
    lines += [_summary_line(p) for p in files[: max(1, limit)]]
    return "\n".join(lines)


@tool
def get_recording(id: str = "latest", start_sec: float | None = None,
                  end_sec: float | None = None) -> str:
    """録音を読みやすいテキストで返します。

    冒頭に要約（音域・音数・打鍵数・密度・ペダル）。続いて時刻順の行で、40ms 以内に鳴った音は和音として
    1行（低い順の音名、前の打鍵からの間隔、各音の長さとベロシティ、BPM設定時は [小節:拍]）、ペダルは別行。
    打鍵が400回を超える録音は、範囲指定がなければ区間ごとの概要（音数・音域・よく出る音）を返します。
    id: "latest" または list_recordings の ID。start_sec/end_sec（秒）で範囲を絞ると、その区間の各音を返します。
    """
    p = storage.resolve(id)
    rec = analyze(p)
    title = f"{p.stem}（{storage.recorded_at(p):%Y-%m-%d %H:%M:%S}）"
    if storage.is_kept(p):
        title += " [保存印]"
    return format_recording(rec, title, start_sec, end_sec)


class NoteSpec(BaseModel):
    note: str | int = Field(description="音名（C4=中央のド, F#3, Bb4）またはMIDI番号")
    start: float = Field(ge=0, description="開始位置（拍、0始まり）")
    duration: float = Field(gt=0, description="長さ（拍）")
    velocity: int = Field(default=80, ge=1, le=127, description="強さ 1–127")


class PedalSpec(BaseModel):
    start: float = Field(ge=0, description="踏む位置（拍）")
    end: float = Field(gt=0, description="離す位置（拍）")


def _build_events(notes: list[NoteSpec], pedal: list[PedalSpec], bpm: float):
    spb = 60.0 / bpm
    events: list[tuple[float, mido.Message]] = []
    for n in notes:
        pitch = parse_note(n.note)
        events.append((n.start * spb, mido.Message("note_on", note=pitch, velocity=n.velocity)))
        events.append(((n.start + n.duration) * spb, mido.Message("note_off", note=pitch, velocity=0)))
    for p in pedal:
        if p.end <= p.start:
            raise ValueError(f"ペダルの end は start より後にしてください: {p}")
        events.append((p.start * spb, mido.Message("control_change", control=64, value=127)))
        events.append((p.end * spb, mido.Message("control_change", control=64, value=0)))
    # note_off / pedal-up before note_on at the same instant
    def prio(m):
        return 0 if (m.type == "note_off" or (m.type == "control_change" and m.value == 0)) else 1
    events.sort(key=lambda e: (e[0], prio(e[1])))
    return events


class PortMissing(Exception):
    def __init__(self, available: list[str]) -> None:
        self.available = available


def _play(events, port_wanted: str, post_mute: float) -> str:
    out = rtmidi.MidiOut(rtmidi.API_MACOSX_CORE, name="PianoTalk Player")
    names = out.get_ports()
    idx = config.find_port(names, port_wanted)
    if idx is None:
        del out
        raise PortMissing(names)
    out.open_port(idx, "PianoTalk Player Out")
    total = events[-1][0] if events else 0.0
    try:
        with lock.playing(total, post_mute):
            time.sleep(0.05)  # let the recorder see the lock before the first note
            t0 = time.perf_counter()
            for t, msg in events:
                delay = t0 + t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                out.send_message(msg.bytes())
            out.send_message([0xB0, 64, 0])   # sustain off
            out.send_message([0xB0, 123, 0])  # all notes off
            time.sleep(0.05)
    finally:
        out.close_port()
        del out
    return f"{names[idx]} で再生しました（{total:.1f} 秒）。"


@tool
async def play_notes(notes: list[NoteSpec], bpm: float | None = None,
                     pedal: list[PedalSpec] | None = None) -> str:
    """フレーズをピアノのMIDI出力へ送って鳴らし、suggestions フォルダに .mid として保存します。

    notes: [{"note": "E4", "start": 0, "duration": 1, "velocity": 80}, ...]（start/duration は拍）。
    和音は同じ start の音を並べます。pedal: [{"start": 0, "end": 4}]（任意、拍）。
    bpm 省略時は設定の default_bpm。bpm=60 なら 1拍=1秒なので、録音の秒数をそのまま使えます。
    再生が終わるまで戻りません。再生中と直後はユーザーの演奏を録音しません。
    """
    cfg = config.load()
    if not notes:
        raise ToolError("notes が空のため再生していません。")
    bpm = bpm or cfg.default_bpm
    if not 20 <= bpm <= 400:
        raise ToolError(f"bpm={bpm:g} は範囲外のため再生していません（20–400 で指定してください）。")
    events = _build_events(notes, pedal or [], bpm)
    total = events[-1][0]
    if total > cfg.max_play_seconds:
        raise ToolError(f"長すぎるため再生していません（{total:.1f} 秒 > 上限 {cfg.max_play_seconds:g} 秒）。"
                        "フレーズを分割して、複数回に分けて鳴らしてください。")
    path = storage.new_suggestion_path()
    write_midi(events, path, bpm, track_name=f"Suggestion {datetime.now():%Y-%m-%d %H:%M}")
    saved = f"フレーズは {path} に保存してあります（{len(notes)}音・BPM {bpm:g}）。"
    try:
        played = await anyio.to_thread.run_sync(_play, events, cfg.output_port, cfg.post_play_mute_seconds)
    except PortMissing as e:
        avail = "、".join(f"「{n}」" for n in e.available) or "なし"
        raise ToolError(
            f"ピアノ（出力ポート{config.describe_port(cfg.output_port)}）が見つからないため再生しませんでした。"
            "MIDI機器が複数つながっている場合は、config.toml の output_port で指定してもらってください。"
            "ユーザーには何も聞こえていません。ピアノの電源と Bluetooth の接続を確認してもらってください。"
            f"いま見えている出力ポート: {avail}。{saved}") from None
    except Exception as e:
        log.exception("playback failed")
        raise ToolError(f"再生の途中で失敗しました（{e}）。最後まで鳴っていない可能性があります。{saved}") from e
    return f"{played}\n{saved}"


@tool
def keep_recording(id: str) -> str:
    """録音に保存印を付けます（kept フォルダへ移動し、自動削除の対象外にします）。"""
    p = storage.keep(id)
    return f"保存印を付けました: {p.stem}（{p}）"


@tool
def delete_recording(id: str) -> str:
    """録音をゴミ箱（~/.Trash）へ移します。Finder の「戻す」で復元できます。"""
    p, where = storage.delete(id)
    return f"ゴミ箱へ移しました: {p.name}" + (f"（{where}）" if where else "")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # stderr only; stdout is the MCP channel
    config.ensure_default_config()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
