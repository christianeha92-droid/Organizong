from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Settings
from .utils import sha256_file

ProgressCallback = Callable[[int, int, str], None]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class RunJournal:
    settings: Settings
    operation: str
    run_id: str
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _append(self, payload: dict[str, object]) -> None:
        payload.setdefault("at", _now())
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()

    def log_move(self, source: Path, target: Path) -> None:
        self._append({"type": "move", "source": str(source), "target": str(target)})

    def log_created(self, path: Path, digest: str | None = None) -> None:
        self._append({"type": "created", "path": str(path), "sha256": digest})

    def log_backup(self, original: Path, backup: Path) -> None:
        self._append({"type": "backup", "original": str(original), "backup": str(backup)})

    def log_mkdir(self, path: Path) -> None:
        self._append({"type": "mkdir", "path": str(path)})

    def log_note(self, message: str) -> None:
        self._append({"type": "note", "message": message})

    def backup_file(self, path: Path) -> Path | None:
        if not path.exists():
            return None
        relative = path.relative_to(self.settings.root) if path.is_relative_to(self.settings.root) else Path(path.name)
        backup = self.settings.backup_dir / self.run_id / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            stem, suffix = backup.stem, backup.suffix
            number = 2
            while backup.exists():
                backup = backup.with_name(f"{stem} ({number}){suffix}")
                number += 1
        shutil.copy2(path, backup)
        self.log_backup(path, backup)
        return backup


def start_run(settings: Settings, operation: str) -> RunJournal:
    settings.ensure_state_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{operation}"
    path = settings.journal_dir / f"{run_id}.jsonl"
    suffix = 2
    while path.exists():
        path = settings.journal_dir / f"{run_id}-{suffix}.jsonl"
        suffix += 1
    journal = RunJournal(settings=settings, operation=operation, run_id=path.stem, path=path)
    journal._append({
        "type": "header",
        "operation": operation,
        "root": str(settings.root),
        "created_at": _now(),
    })
    return journal


def _is_undone(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "undone":
                    return True
    except OSError:
        return True
    return False


def latest_run(settings: Settings, include_undone: bool = False) -> Path | None:
    if not settings.journal_dir.exists():
        return None
    candidates = sorted(
        settings.journal_dir.glob("*.jsonl"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if include_undone or not _is_undone(candidate):
            return candidate
    return None


def _read_actions(path: Path) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") in {"move", "created", "backup", "mkdir", "undone"}:
                    actions.append(payload)
    except OSError:
        pass
    return actions


def undo_run(
    settings: Settings,
    journal_path: Path,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    actions = _read_actions(journal_path)
    if any(action.get("type") == "undone" for action in actions):
        return {"undone": 0, "skipped": 0, "errors": 0, "already": 1}
    actionable = [action for action in actions if action.get("type") != "undone"]
    counts = {"undone": 0, "skipped": 0, "errors": 0}
    total = len(actionable)
    created_dirs: list[Path] = []

    for index, action in enumerate(reversed(actionable), start=1):
        kind = action.get("type")
        try:
            if kind == "created":
                target = Path(str(action.get("path", "")))
                if target.exists():
                    expected = action.get("sha256")
                    if expected and sha256_file(target) != expected:
                        counts["skipped"] += 1
                    else:
                        target.unlink()
                        counts["undone"] += 1
                else:
                    counts["skipped"] += 1
            elif kind == "backup":
                original = Path(str(action.get("original", "")))
                backup = Path(str(action.get("backup", "")))
                if backup.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup), str(original))
                    counts["undone"] += 1
                else:
                    counts["skipped"] += 1
            elif kind == "move":
                source = Path(str(action.get("source", "")))
                target = Path(str(action.get("target", "")))
                if target.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(source))
                    counts["undone"] += 1
                else:
                    counts["skipped"] += 1
            elif kind == "mkdir":
                created_dirs.append(Path(str(action.get("path", ""))))
                counts["undone"] += 1
        except OSError:
            counts["errors"] += 1
        if progress:
            progress(index, total, kind or "")

    for directory in created_dirs:
        try:
            directory.rmdir()
        except OSError:
            pass

    with journal_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "undone", "at": _now()}, ensure_ascii=False) + "\n")
    return counts
