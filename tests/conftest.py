import os
from pathlib import Path

import pytest

CONFIG = """\
[midi]
input_port = "{inp}"
output_port = "{out}"
[recording]
silence_seconds = {silence}
bpm = {bpm}
min_notes = 3
min_duration_seconds = 2.0
retention_days = 30
[playback]
default_bpm = 120
post_play_mute_seconds = 0.5
[ui]
menubar = false
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated PIANOTALK_HOME. Real trash is replaced by a local folder."""
    h = tmp_path / "PianoTalk"
    h.mkdir()
    monkeypatch.setenv("PIANOTALK_HOME", str(h))
    trash = tmp_path / "Trash"
    trash.mkdir()

    from pianotalk import storage

    def fake_trash(p: Path):
        dest = trash / p.name
        p.replace(dest)
        return dest

    monkeypatch.setattr(storage, "trash", fake_trash)
    return h


@pytest.fixture
def trash_dir(home):
    return home.parent / "Trash"


def write_config(home: Path, inp="PianoTalk Test Piano", out="PianoTalk Test Synth",
                 silence=0.6, bpm=0):
    (home / "config.toml").write_text(CONFIG.format(inp=inp, out=out, silence=silence, bpm=bpm))
