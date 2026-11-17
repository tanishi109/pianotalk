"""Pack dist/pianotalk-<version>.mcpb for Claude desktop.

A .mcpb is a zip with manifest.json at the root. This is a uv-type bundle:
it ships the source, pyproject.toml and uv.lock, and Claude desktop builds the
environment with uv on install (it downloads uv itself if the Mac has none).
"""
from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_FILES = ["pyproject.toml", "uv.lock", ".python-version", "README.md", "LICENSE"]


def bundle_files() -> list[tuple[Path, str]]:
    files = [(ROOT / "mcpb" / n, n) for n in ("manifest.json", "mcpb_main.py")]
    files += [(ROOT / n, n) for n in TOP_FILES]
    files += [(p, p.relative_to(ROOT).as_posix()) for p in sorted((ROOT / "pianotalk").glob("*.py"))]
    return files


def check_versions() -> str:
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if manifest["version"] != project["version"]:
        sys.exit(f"version mismatch: manifest {manifest['version']} vs pyproject {project['version']}")
    return manifest["version"]


def build(out_dir: Path = ROOT / "dist") -> Path:
    version = check_versions()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"pianotalk-{version}.mcpb"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, name in bundle_files():
            z.write(src, name)
    return out


if __name__ == "__main__":
    out = build()
    print(out)
    for i in zipfile.ZipFile(out).infolist():
        print(f"  {i.file_size:7d}  {i.filename}")
