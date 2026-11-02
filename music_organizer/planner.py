from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .models import MediaInfo, OrganizePlan, PlanItem, Track
from .utils import (
    fit_path,
    format_track_filename,
    normalize_match,
    path_key,
    sha256_file,
)

ProgressCallback = Callable[[int, int, str], None]

_ALBUM_COVER_NAMES = {
    "cover", "folder", "front", "album", "albumart", "album art", "caratula", "portada",
}
_COMPANION_EXTENSIONS = {".lrc", ".jpg", ".jpeg", ".png", ".webp"}


def artist_for_folder(track: Track, settings: Settings) -> str:
    if settings.artist_field == "artist":
        return track.artist or track.album_artist or "Unknown Artist"
    return track.album_artist or track.artist or "Unknown Artist"


def _group_key(track: Track, settings: Settings) -> tuple[str, str]:
    return (
        normalize_match(artist_for_folder(track, settings)),
        normalize_match(track.album or "Unknown Album"),
    )


def _find_companions(audio: Path) -> list[Path]:
    if not audio.parent.exists():
        return []
    result: list[Path] = []
    audio_stem = audio.stem.casefold()
    for candidate in audio.parent.iterdir():
        if not candidate.is_file() or candidate == audio:
            continue
        if candidate.suffix.lower() not in _COMPANION_EXTENSIONS:
            continue
        if candidate.stem.casefold() != audio_stem:
            continue
        if candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            if candidate.stem.casefold() in _ALBUM_COVER_NAMES:
                continue
        result.append(candidate)
    return sorted(result, key=lambda item: (item.suffix.lower() != ".lrc", str(item).casefold()))


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        if not left.exists() or not right.exists():
            return False
        if left.stat().st_size != right.stat().st_size:
            return False
        return sha256_file(left) == sha256_file(right)
    except OSError:
        return False


def _unique_target(target: Path, planned: set[str]) -> Path:
    if path_key(target) not in planned and not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    number = 2
    while True:
        candidate = target.with_name(f"{stem} ({number}){suffix}")
        if path_key(candidate) not in planned and not candidate.exists():
            return candidate
        number += 1


def _companion_target(audio_target: Path, companion: Path) -> Path:
    if companion.suffix.lower() == ".lrc":
        return audio_target.with_suffix(".lrc")
    return audio_target.with_name(f"{audio_target.stem}{companion.suffix.lower()}")


def _media_info(path: Path, *, duration: float | None = None, bitrate: int | None = None) -> MediaInfo:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return MediaInfo(
        path=path,
        format=path.suffix.lower().lstrip("."),
        size=size,
        duration=duration,
        bitrate=bitrate,
    )


def _track_info(track: Track) -> MediaInfo:
    return _media_info(track.path, duration=track.duration, bitrate=track.bitrate)


def _alternate_album_target(
    settings: Settings,
    artist: str,
    album: str,
    filename: str,
    planned: set[str],
    alternate_folders: dict[tuple[str, str], Path],
    group_key: tuple[str, str],
) -> Path:
    folder = alternate_folders.get(group_key)
    if folder is None:
        used_folders = {path_key(path) for path in alternate_folders.values()}
        number = 2
        while True:
            alternate_album = f"{album} ({number})"
            candidate = fit_path(
                settings.library_root, artist, alternate_album, filename, settings.max_path,
            )
            folder = candidate.parent
            if not folder.exists() and path_key(folder) not in used_folders:
                break
            number += 1
        alternate_folders[group_key] = folder

    target = fit_path(
        settings.library_root, artist, folder.name, filename, settings.max_path,
    )
    if path_key(target.parent) != path_key(folder):
        target = folder / Path(filename).name
    if path_key(target) in planned or target.exists():
        target = _unique_target(target, planned)
    return target


def build_plan(
    settings: Settings,
    tracks: list[Track],
    progress: ProgressCallback | None = None,
) -> OrganizePlan:
    plan = OrganizePlan(root=settings.root, library_root=settings.library_root, tracks=tracks)
    planned_audio: set[str] = set()
    planned_companions: set[str] = set()
    alternate_folders: dict[tuple[str, str], Path] = {}

    group_discs: dict[tuple[str, str], list[int]] = defaultdict(list)
    for track in tracks:
        if track.disc_number:
            group_discs[_group_key(track, settings)].append(track.disc_number)
    multi_disc = {
        key: (max(discs) > 1 if discs else False)
        for key, discs in group_discs.items()
    }

    total = len(tracks)
    for index, track in enumerate(tracks, start=1):
        if not track.path.exists():
            plan.items.append(PlanItem(
                kind="audio", status="missing", source=track.path, target=track.path,
                track=track, reason="el archivo ya no existe",
            ))
            if progress:
                progress(index, total, str(track.path))
            continue

        artist = artist_for_folder(track, settings)
        album = track.album or "Unknown Album"
        key = _group_key(track, settings)
        filename = format_track_filename(
            track.title,
            track.path.suffix.lower(),
            track.track_number,
            track.disc_number,
            multi_disc.get(key, False),
        )
        target = fit_path(settings.library_root, artist, album, filename, settings.max_path)
        source = track.path
        alternate_target: Path | None = None
        target_info: MediaInfo | None = None

        if path_key(source) == path_key(target):
            status = "same"
            reason = "ya está en su carpeta correcta"
        elif path_key(target) in planned_audio:
            target = _unique_target(target, planned_audio)
            status = "rename"
            reason = "otra pista tiene el mismo destino; se añadió un número"
        elif target.exists():
            target_info = _media_info(target)
            if _same_file_content(source, target):
                status = "duplicate"
                reason = "ya existe un archivo idéntico en el destino"
            else:
                status = "conflict"
                reason = "el destino ya existe y es distinto; requiere decisión"
                alternate_target = _alternate_album_target(
                    settings, artist, album, filename, planned_audio,
                    alternate_folders, key,
                )
        else:
            status = "move"
            reason = "se moverá a artista/álbum"

        planned_audio.add(path_key(target))
        if alternate_target:
            planned_audio.add(path_key(alternate_target))
        plan.target_for_track[source] = target
        plan.items.append(PlanItem(
            kind="audio", status=status, source=source, target=target,
            track=track, reason=reason, alternate_target=alternate_target,
            source_info=_track_info(track), target_info=target_info,
        ))

        if status not in {"move", "rename", "conflict"}:
            if progress:
                progress(index, total, str(track.path))
            continue

        for companion in _find_companions(source):
            companion_target = _companion_target(target, companion)
            companion_alternate = (
                _companion_target(alternate_target, companion)
                if alternate_target else None
            )
            companion_info: MediaInfo | None = None

            if status == "conflict":
                if companion_target.exists():
                    companion_info = _media_info(companion_target)
                    c_status = "conflict"
                    c_reason = "el destino del acompañante ya existe; se resuelve con la canción"
                else:
                    c_status = "move"
                    c_reason = "acompañante de la canción; sigue la decisión del conflicto"
            elif path_key(companion_target) in planned_companions:
                c_status = "conflict"
                c_reason = "otro archivo ya ocupa el nombre del acompañante"
            elif companion_target.exists():
                companion_info = _media_info(companion_target)
                if _same_file_content(companion, companion_target):
                    c_status = "duplicate"
                    c_reason = "el archivo de letra/imagen ya existe en el destino"
                else:
                    c_status = "conflict"
                    c_reason = "el destino del acompañante ya existe; se deja el original"
            else:
                c_status = "move"
                c_reason = "acompañante de la canción"

            if c_status == "move":
                planned_companions.add(path_key(companion_target))
            if companion_alternate:
                planned_companions.add(path_key(companion_alternate))
            plan.items.append(PlanItem(
                kind="companion", status=c_status, source=companion,
                target=companion_target, track=track, reason=c_reason,
                source_info=_media_info(companion),
                alternate_target=companion_alternate, target_info=companion_info,
                audio_source=source,
            ))

        if progress:
            progress(index, total, str(track.path))

    return plan
