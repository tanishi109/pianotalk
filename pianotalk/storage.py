"""Recording files: naming, lookup, keep/delete, retention cleanup."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
STAMP = "%Y%m%d-%H%M%S"


def new_recording_path(started: datetime) -> Path:
    d = config.recordings_dir() / started.strftime("%Y-%m-%d")
    stem = started.strftime(STAMP)
    p = d / f"{stem}.mid"
    i = 2
    while p.exists():
        p = d / f"{stem}-{i}.mid"
        i += 1
    return p


def new_suggestion_path() -> Path:
    d = config.suggestions_dir()
    stem = datetime.now().strftime(STAMP)
    p = d / f"{stem}.mid"
    i = 2
    while p.exists():
        p = d / f"{stem}-{i}.mid"
        i += 1
    return p


def all_recordings() -> list[Path]:
    """All recordings (including kept), newest first."""
    root = config.recordings_dir()
    if not root.exists():
        return []
    files = [p for p in root.rglob("*.mid") if p.is_file()]
    return sorted(files, key=lambda p: (p.stem, p.stat().st_mtime), reverse=True)


def all_suggestions() -> list[Path]:
    """Phrases played by Claude (suggestions/), newest first."""
    d = config.suggestions_dir()
    if not d.exists():
        return []
    return sorted((p for p in d.glob("*.mid") if p.is_file()), key=lambda p: p.stem, reverse=True)


def is_kept(p: Path) -> bool:
    try:
        p.resolve().relative_to(config.kept_dir().resolve())
        return True
    except ValueError:
        return False


def recorded_at(p: Path) -> datetime:
    try:
        return datetime.strptime(p.stem[:15], STAMP)
    except ValueError:
        return datetime.fromtimestamp(p.stat().st_mtime)


def resolve(rec_id: str) -> Path:
    """'latest', or a file name with/without .mid (only basenames are matched)."""
    files = all_recordings()
    if not files:
        raise FileNotFoundError("録音がまだありません")
    key = (rec_id or "latest").strip()
    if key.lower() == "latest":
        return files[0]
    name = Path(key).name
    if name.endswith(".mid"):
        name = name[:-4]
    for p in files:
        if p.stem == name:
            return p
    raise FileNotFoundError(f"録音が見つかりません: {rec_id}（list_recordings で ID を確認してください）")


def trash(p: Path) -> Path | None:
    """Move to the user's Trash (Finder 'Put Back' works). Returns the new location if known."""
    try:
        from Foundation import NSURL, NSFileManager

        ok, url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(p)), None, None)
        if ok:
            return Path(url.path()) if url is not None else None
        log.warning("NSFileManager trash failed for %s: %s; falling back", p, err)
    except Exception as e:  # pragma: no cover - pyobjc missing
        log.warning("NSFileManager unavailable (%s); falling back", e)
    dest_dir = Path.home() / ".Trash"
    dest = dest_dir / p.name
    if dest.exists():
        dest = dest_dir / f"{p.stem} {time.strftime('%H.%M.%S')}{p.suffix}"
    shutil.move(str(p), dest)
    return dest


def keep(rec_id: str) -> Path:
    p = resolve(rec_id)
    if is_kept(p):
        return p
    dest = config.kept_dir() / p.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"kept に同名ファイルがあります: {dest.name}")
    p.replace(dest)
    _remove_empty_dir(p.parent)
    return dest


def delete(rec_id: str) -> tuple[Path, Path | None]:
    p = resolve(rec_id)
    where = trash(p)
    _remove_empty_dir(p.parent)
    return p, where


def cleanup(retention_days: int, now: datetime | None = None) -> list[Path]:
    """Trash non-kept recordings older than retention_days. Returns trashed paths."""
    if retention_days <= 0:
        return []
    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    removed = []
    for p in all_recordings():
        if is_kept(p) or recorded_at(p) >= cutoff:
            continue
        try:
            trash(p)
            removed.append(p)
            _remove_empty_dir(p.parent)
        except Exception:
            log.exception("failed to trash %s", p)
    return removed


def _remove_empty_dir(d: Path) -> None:
    root = config.recordings_dir().resolve()
    try:
        if d.resolve() != root and d.resolve() != config.kept_dir().resolve() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass
