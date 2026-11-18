"""Recorder presence: the recorder (writer) tells the MCP server (reader) it is alive.

The recorder keeps ~/PianoTalk/recorder.json up to date:
    {"pid": ..., "updated": epoch, "port": "CN201 ..." | null, "paused": bool,
     "wanted": "「CN201」", "ambiguous": 0}
It rewrites the file when its state changes and at least every HEARTBEAT
seconds, and removes it on a clean exit. The MCP server uses it to warn Claude
when what the user plays is not being recorded (recorder not running, paused,
or no piano connected), so Claude can tell the user instead of reading stale takes.
"""

from __future__ import annotations

import json
import os
import time

from . import config
from .lock import _pid_alive

HEARTBEAT = 10.0
STALE = 60.0  # no update for this long: the recorder is hung or gone

START_HINT = (
    "メニューバーにピアノのアイコンが出ていなければ止まっています。"
    "PianoTalk Recorder アプリを Launchpad や Spotlight から開けば録音が始まります"
    "（メニューの「ログイン時に起動」をオンにしておくと、次からは自動で起動します）。"
    "まだセットアップしていなければ README の手順（https://github.com/tanishi109/pianotalk#setup）を案内してください。"
)


def write(state: dict) -> None:
    p = config.recorder_status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "updated": time.time(), **state}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def clear() -> None:
    """Remove the file on a clean exit (only if it is still ours)."""
    p = config.recorder_status_path()
    try:
        if json.loads(p.read_text(encoding="utf-8")).get("pid") == os.getpid():
            p.unlink()
    except (OSError, ValueError):
        pass


def read() -> dict | None:
    try:
        return json.loads(config.recorder_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def warning(now: float | None = None) -> str | None:
    """A note for Claude when the user's playing is not being recorded, else None."""
    now = time.time() if now is None else now
    d = read()
    pid = int(d.get("pid") or 0) if d else 0
    if pid <= 0 or not _pid_alive(pid):  # (kill(0, ...) would signal our own group)
        return ("【注意】録音デーモン（pianotalk-recorder）が動いていないため、いまピアノを弾いても録音されません。"
                "読めるのは過去の録音だけです。ユーザーに伝えて起動してもらってください。" + START_HINT)
    if now - float(d.get("updated", 0)) > STALE:
        return ("【注意】録音デーモンが応答していません（最後の応答から"
                f"{int(now - float(d.get('updated', 0)))}秒）。録音できていない可能性があるので、"
                "ユーザーに録音デーモンを再起動してもらってください。" + START_HINT)
    if d.get("paused"):
        return ("【注意】録音は一時停止中です。いまピアノを弾いても録音されません。"
                "再開はメニューバーのピアノのアイコン →「録音を再開」です。")
    if not d.get("port"):
        if d.get("ambiguous"):
            return ("【注意】MIDI機器が複数つながっていて、どれがピアノか決められないため録音していません。"
                    "~/PianoTalk/config.toml の input_port / output_port にピアノのポート名の一部を書き、"
                    "録音デーモンを再起動してもらってください。")
        return (f"【注意】ピアノ（{d.get('wanted', 'MIDI機器')}）が接続されていないため、いまは録音されていません。"
                "ピアノの電源と、Bluetooth MIDI の場合は接続（Audio MIDI設定 → MIDIスタジオ）を確認してもらってください。")
    return None
