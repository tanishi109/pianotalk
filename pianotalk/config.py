"""Configuration and well-known paths.

All state lives under ~/PianoTalk (override with the PIANOTALK_HOME env var,
which the tests use). The config file is ~/PianoTalk/config.toml.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_TOML = """\
# PianoTalk 設定ファイル
# 変更後は録音デーモンを再起動してください:
#   PianoTalk Recorder アプリ: メニューバーのアイコン →「終了」のあと、開き直す
#   launchd で動かしている場合: launchctl kickstart -k gui/$(id -u)/com.pianotalk.recorder
# MCPサーバーは Claude 側で再接続（アプリ再起動）すると反映されます。

[midi]
# ポート名は部分一致（大文字小文字を区別しない）。
# 空のときは、MIDI機器が1台だけつながっていればそれを自動で使います
# （IAC・ネットワークセッション・PianoTalk 自身の仮想ポートは数えません）。
# 複数つながっている場合は、名前の一部を書いてください。
# 実際の名前は `.venv/bin/pianotalk-ports` で確認できます。
input_port = ""
output_port = ""

[recording]
# 全キーとペダルが離れてから、この秒数だけ無音が続いたら録音終了
silence_seconds = 3.0
# BPM（0 = 未設定）。設定するとMIDIファイルのテンポに反映され、
# get_recording で小節・拍の位置も表示されます。
bpm = 0
# 1小節の拍数（BPM設定時の小節・拍表示に使用）
beats_per_bar = 4
# これ未満の録音は保存しない
min_notes = 3
min_duration_seconds = 2.0
# 保存期間（日）。過ぎた録音はゴミ箱へ（kept フォルダ内は除外）
retention_days = 30

[playback]
# play_notes で bpm を省略したときの既定値
default_bpm = 100
# 再生終了後、録音を無視し続ける秒数（ピアノが受信MIDIを送り返す対策）
post_play_mute_seconds = 1.0
# 1回の再生の最大長（秒）
max_seconds = 120

[ui]
# 録音デーモンの起動中にメニューバーへアイコンを出す
menubar = true
"""


def home() -> Path:
    return Path(os.environ.get("PIANOTALK_HOME", Path.home() / "PianoTalk")).expanduser()


def config_path() -> Path:
    return home() / "config.toml"


def recordings_dir() -> Path:
    return home() / "recordings"


def kept_dir() -> Path:
    return recordings_dir() / "kept"


def suggestions_dir() -> Path:
    return home() / "suggestions"


def logs_dir() -> Path:
    return home() / "logs"


def lock_path() -> Path:
    return home() / "playing.lock"


def recorder_status_path() -> Path:
    return home() / "recorder.json"


def recorder_lock_path() -> Path:
    return home() / "recorder.lock"


@dataclass
class Config:
    input_port: str = ""
    output_port: str = ""
    silence_seconds: float = 3.0
    bpm: float | None = None
    beats_per_bar: int = 4
    min_notes: int = 3
    min_duration_seconds: float = 2.0
    retention_days: int = 30
    default_bpm: float = 100.0
    post_play_mute_seconds: float = 1.0
    max_play_seconds: float = 120.0
    menubar: bool = True


def ensure_default_config() -> Path:
    p = config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return p


def load() -> Config:
    p = config_path()
    data = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    midi = data.get("midi", {})
    rec = data.get("recording", {})
    play = data.get("playback", {})
    ui = data.get("ui", {})
    c = Config()
    c.input_port = str(midi.get("input_port", c.input_port))
    c.output_port = str(midi.get("output_port", c.output_port))
    c.silence_seconds = float(rec.get("silence_seconds", c.silence_seconds))
    bpm = float(rec.get("bpm", 0) or 0)
    c.bpm = bpm if bpm > 0 else None
    c.beats_per_bar = int(rec.get("beats_per_bar", c.beats_per_bar))
    c.min_notes = int(rec.get("min_notes", c.min_notes))
    c.min_duration_seconds = float(rec.get("min_duration_seconds", c.min_duration_seconds))
    c.retention_days = int(rec.get("retention_days", c.retention_days))
    c.default_bpm = float(play.get("default_bpm", c.default_bpm))
    c.post_play_mute_seconds = float(play.get("post_play_mute_seconds", c.post_play_mute_seconds))
    c.max_play_seconds = float(play.get("max_seconds", c.max_play_seconds))
    c.menubar = bool(ui.get("menubar", c.menubar))
    return c


# Ports never chosen automatically (macOS built-ins and our own virtual ports).
AUTO_SKIP = ("iac", "network", "session", "pianotalk")


def auto_candidates(names: list[str]) -> list[int]:
    # CoreMIDI can report a port with no name (None) while a device is (dis)connecting
    return [i for i, n in enumerate(names)
            if n and not any(s in n.casefold() for s in AUTO_SKIP)]


def find_port(names: list[str], wanted: str) -> int | None:
    """Index of the first port whose name contains `wanted` (case-insensitive).

    An empty `wanted` means "auto": the only port that looks like a real MIDI
    device, or None when there are zero or several.
    """
    w = wanted.strip().casefold()
    if not w:
        cands = auto_candidates(names)
        return cands[0] if len(cands) == 1 else None
    for i, n in enumerate(names):
        if n and w in n.casefold():
            return i
    return None


def describe_port(wanted: str) -> str:
    """How to name the wanted port in messages."""
    return f"「{wanted}」" if wanted.strip() else "MIDI機器（自動選択）"
