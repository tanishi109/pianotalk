"""py2app build for PianoTalk Recorder.app — run via scripts/build-recorder-app.sh."""
import tomllib
from pathlib import Path

from setuptools import setup

# uv's Python (python-build-standalone) has zlib built in, but py2app copies
# zlib.__file__ next to the stdlib zip. Skip that copy when there is no file.
import sys
import zlib

if "zlib" in sys.builtin_module_names:
    from py2app import build_app

    zlib.__file__ = None
    _copy_file = build_app.py2app.copy_file
    build_app.py2app.copy_file = lambda self, src, *a, **kw: None if src is None else _copy_file(self, src, *a, **kw)

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

setup(
    name="PianoTalk Recorder",
    app=["recorder_app.py"],
    options={"py2app": {
        "packages": ["pianotalk", "mido", "rtmidi"],
        "includes": ["AppKit", "Foundation", "objc"],
        "excludes": ["mcp", "pydantic", "anyio", "starlette", "uvicorn", "pytest", "tkinter"],
        "plist": {
            "CFBundleName": "PianoTalk Recorder",
            "CFBundleDisplayName": "PianoTalk Recorder",
            "CFBundleIdentifier": "io.github.tanishi109.pianotalk.recorder",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # menu bar only, no Dock icon
            "LSMinimumSystemVersion": "13.0",
            "NSHumanReadableCopyright": "MIT License",
            # Apps start with an ASCII locale; keep Python's text I/O in UTF-8 anyway.
            "LSEnvironment": {"PYTHONUTF8": "1"},
        },
    }},
)
