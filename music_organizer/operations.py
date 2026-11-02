from __future__ import annotations

import csv
import shutil
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import Settings
from .journal import latest_run, start_run, undo_run
from .models import OperationSummary, OrganizePlan, PlanItem
from .utils import path_key, remove_empty_dirs

ProgressCallback = Callable[[int, int, str], None]
ConflictResolver = Callable[[PlanItem], str]


def _ensure_parent(target: Path, settings: Settings, journal) -> None:
    missing: list[Path] = []
    current = target.parent
    while not current.exists() and path_key(current) != path_key(settings.library_root):
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        journal.log_mkdir(directory)
    target.parent.mkdir(parents=True, exist_ok=True)


def _unique_file_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    number = 2
    while True:
        candidate = target.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def _decision_for(
    item: PlanItem,
    settings: Settings,
    decisions: dict[str, str] | None,
    resolver: ConflictResolver | None,
) -> str:
    if decisions:
        chosen = decisions.get(path_key(item.source), "")
        if chosen in {"overwrite", "keep", "skip"}:
            return chosen
    if resolver:
        chosen = resolver(item)
        if chosen in {"overwrite", "keep", "skip"}:
            return chosen
    if settings.conflict in {"overwrite", "keep", "skip"}:
        return settings.conflict
    return "keep"


def _move_one(
    item: PlanItem,
    target: Path,
    settings: Settings,
    journal,
    summary: OperationSummary,
    *,
    overwrite: bool = False,
    status_label: str = "movido",
) -> bool:
    source = item.source
    if not source.exists():
        summary.errors += 1
        summary.details.append(f"Ya no existe: {source}")
        summary.add("error")
        return False
    if path_key(source) == path_key(target):
        summary.skipped += 1
        summary.add("same")
        return True
    if target.exists():
        if not overwrite:
            summary.errors += 1
            summary.details.append(f"El destino apareció durante la operación: {target}")
            summary.add("error")
            return False
        if target.is_dir():
            summary.errors += 1
            summary.details.append(f"El destino es una carpeta, no un archivo: {target}")
            summary.add("error")
            return False
    try:
        _ensure_parent(target, settings, journal)
        if target.exists():
            journal.backup_file(target)
            target.unlink()
        shutil.move(str(source), str(target))
        journal.log_move(source, target)
        summary.changed += 1
        summary.add(status_label)
        return True
    except OSError as exc:
        summary.errors += 1
        summary.details.append(f"No se pudo mover {source}: {exc}")
        summary.add("error")
        return False


def apply_plan(
    plan: OrganizePlan,
    settings: Settings,
    progress: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
    decisions: dict[str, str] | None = None,
    conflict_resolver: ConflictResolver | None = None,
) -> OperationSummary:
    summary = OperationSummary(operation="organize")
    audio_changes = [item for item in plan.audio_items if item.will_change]
    companion_changes = [item for item in plan.companion_items if item.will_change]
    total = len(audio_changes) + len(companion_changes)
    if not total:
        for item in plan.items:
            summary.add(item.status)
        summary.skipped = summary.total
        summary.report_path = write_plan_report(settings, plan, "plan")
        return summary

    settings.ensure_state_dirs()
    settings.library_root.mkdir(parents=True, exist_ok=True)
    journal = start_run(settings, "organize")
    summary.journal_path = journal.path
    done = 0
    moved_audio: set[str] = set()
    decisions_by_audio: dict[str, str] = {}

    for item in audio_changes:
        if stop_event and stop_event.is_set():
            summary.details.append("Operación detenida por el usuario.")
            break
        target = item.target
        overwrite = False
        status_label = "movido"
        if item.status == "conflict":
            decision = _decision_for(item, settings, decisions, conflict_resolver)
            decisions_by_audio[path_key(item.source)] = decision
            if decision == "skip":
                summary.skipped += 1
                summary.add("omitido")
                summary.details.append(
                    f"Conflicto omitido: {item.source.name} (se conserva el destino)"
                )
                done += 1
                if progress:
                    progress(done, total, str(item.source))
                continue
            if decision == "keep":
                target = item.alternate_target or _unique_file_target(item.target)
                if target.exists():
                    target = _unique_file_target(target)
                status_label = "guardado_aparte"
                summary.details.append(
                    f"Conflicto conservado aparte: {item.source.name} -> {target}"
                )
            else:
                overwrite = True
                status_label = "sobrescrito"
                summary.details.append(
                    f"Conflicto sobrescrito: {item.source.name} (copia del destino guardada)"
                )
        if _move_one(
            item, target, settings, journal, summary,
            overwrite=overwrite, status_label=status_label,
        ):
            moved_audio.add(path_key(item.source))
        done += 1
        if progress:
            progress(done, total, str(item.source))

    for item in companion_changes:
        if stop_event and stop_event.is_set():
            break
        audio_source = item.audio_source or (item.track.path if item.track else None)
        decision = decisions_by_audio.get(path_key(audio_source)) if audio_source else None
        target = item.target
        overwrite = False
        status_label = "movido"
        if decision == "skip":
            summary.skipped += 1
            summary.add("omitido")
            done += 1
            if progress:
                progress(done, total, str(item.source))
            continue
        if decision == "keep":
            target = item.alternate_target or item.target
            if target.exists():
                target = _unique_file_target(target)
            status_label = "guardado_aparte"
        elif decision == "overwrite":
            overwrite = True
            status_label = "sobrescrito"
        elif item.status == "conflict":
            summary.skipped += 1
            summary.add("omitido")
            summary.details.append(
                f"Acompañante conservado: {item.source.name} (el destino ya existía)"
            )
            done += 1
            if progress:
                progress(done, total, str(item.source))
            continue
        if item.track and path_key(item.track.path) not in moved_audio:
            summary.skipped += 1
            summary.add("omitido")
            done += 1
            if progress:
                progress(done, total, str(item.source))
            continue
        _move_one(
            item, target, settings, journal, summary,
            overwrite=overwrite, status_label=status_label,
        )
        done += 1
        if progress:
            progress(done, total, str(item.source))

    processed = {id(item) for item in audio_changes} | {id(item) for item in companion_changes}
    for item in plan.items:
        if id(item) in processed:
            continue
        summary.add(item.status)
        if item.status not in {"move", "rename", "conflict"}:
            summary.skipped += 1

    if settings.clean_empty_dirs:
        protected = settings.ignore_dirs | {settings.state_dir, settings.library_root}
        removed = remove_empty_dirs(settings.root, protected)
        if removed:
            summary.details.append(f"Carpetas vacías eliminadas: {len(removed)}")

    summary.report_path = write_plan_report(settings, plan, "organize")
    return summary


def write_plan_report(settings: Settings, plan: OrganizePlan, prefix: str) -> Path:
    settings.ensure_state_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.report_dir / f"{prefix}-{stamp}.csv"
    suffix = 2
    while path.exists():
        path = settings.report_dir / f"{prefix}-{stamp}-{suffix}.csv"
        suffix += 1
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow([
            "tipo", "estado", "artista", "album", "titulo",
            "formato_origen", "tamano_origen_bytes",
            "formato_destino", "tamano_destino_bytes",
            "origen", "destino", "destino_alternativo", "motivo",
        ])
        for item in plan.items:
            source_info = item.source_info
            target_info = item.target_info
            writer.writerow([
                item.kind,
                item.status,
                item.track.display_artist if item.track else "",
                item.track.album if item.track else "",
                item.track.title if item.track else "",
                source_info.format if source_info else (item.track.format if item.track else ""),
                source_info.size if source_info else (item.track.size if item.track else ""),
                target_info.format if target_info else "",
                target_info.size if target_info else "",
                str(item.source),
                str(item.target),
                str(item.alternate_target) if item.alternate_target else "",
                item.reason,
            ])
    return path


def undo_last(settings: Settings, progress: ProgressCallback | None = None) -> OperationSummary:
    summary = OperationSummary(operation="undo")
    journal_path = latest_run(settings)
    if journal_path is None:
        summary.details.append("No hay ninguna sesión que deshacer.")
        return summary
    summary.journal_path = journal_path
    counts = undo_run(settings, journal_path, progress)
    summary.total = counts.get("undone", 0) + counts.get("skipped", 0) + counts.get("errors", 0)
    summary.changed = counts.get("undone", 0)
    summary.skipped = counts.get("skipped", 0)
    summary.errors = counts.get("errors", 0)
    if counts.get("undone"):
        summary.add("deshecho", counts["undone"])
    if counts.get("skipped"):
        summary.add("omitido", counts["skipped"])
    if counts.get("errors"):
        summary.add("error", counts["errors"])
    if counts.get("already"):
        summary.details.append("La última sesión ya fue deshecha.")
    else:
        summary.details.append(
            f"Deshecho: {counts.get('undone', 0)}; omitidos: {counts.get('skipped', 0)}; errores: {counts.get('errors', 0)}"
        )
    return summary


def cleanup_empty_dirs(settings: Settings) -> list[Path]:
    protected = settings.ignore_dirs | {settings.state_dir, settings.library_root}
    return remove_empty_dirs(settings.root, protected)
