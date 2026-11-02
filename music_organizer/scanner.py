from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from .config import Settings
from .models import Track
from .utils import (
    format_duration,
    infer_title_from_filename,
    is_generic_folder,
    iter_audio_files,
    normalize_match,
    normalize_text,
    parse_number_pair,
    path_key,
)

ProgressCallback = Callable[[int, int, str], None]


def _first(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        value = value[0]
        if isinstance(value, tuple):
            value = value[0]
    return normalize_text(value)


def _tag_value(tags: object, *keys: str) -> str:
    if tags is None:
        return ""
    for key in keys:
        try:
            if key in tags:  # type: ignore[operator]
                return _first(tags[key])  # type: ignore[index]
        except (TypeError, KeyError, IndexError):
            continue
    return ""


def _vorbis_map(tags: object) -> dict[str, object]:
    result: dict[str, object] = {}
    if tags is None:
        return result
    try:
        for key, value in tags.items():  # type: ignore[union-attr]
            result[str(key).casefold()] = value
    except (AttributeError, TypeError):
        pass
    return result


def _id3_text(tags: ID3 | None, key: str) -> str:
    if tags is None:
        return ""
    frame = tags.get(key)
    if frame is None:
        return ""
    text = getattr(frame, "text", None)
    if isinstance(text, (list, tuple)):
        return normalize_text(" / ".join(str(part) for part in text if str(part).strip()))
    return normalize_text(text)


def _mp4_text(tags: object, key: str) -> str:
    if tags is None:
        return ""
    try:
        value = tags.get(key)  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return ""
    return _first(value)


def _id3_pictures(tags: ID3 | None) -> list[APIC]:
    if tags is None:
        return []
    try:
        return list(tags.getall("APIC"))
    except (AttributeError, TypeError):
        return []


def _has_vorbis_picture(tags: object) -> bool:
    values = _vorbis_map(tags)
    return any(key in values for key in ("metadata_block_picture", "coverart", "coverartmime"))


def _has_embedded_lyrics(tags: object, kind: str) -> bool:
    if kind == "id3":
        if not isinstance(tags, ID3):
            return False
        return bool(tags.getall("USLT") or tags.getall("SYLT"))
    if kind == "mp4":
        return bool(_mp4_text(tags, "\xa9lyr"))
    values = _vorbis_map(tags)
    return any(values.get(key) for key in ("lyrics", "unsyncedlyrics", "syncedlyrics", "©lyr"))


_UNKNOWN_FOLDER_NAMES = {
    "unknown album", "unknown artist", "sin album", "sin álbum", "sin artista",
    "artista desconocido", "album desconocido", "álbum desconocido",
}


def _is_unknown_or_generic(name: str) -> bool:
    return is_generic_folder(name) or normalize_match(name) in _UNKNOWN_FOLDER_NAMES


def _infer_artist_from_path(path: Path, root: Path) -> str:
    parent = path.parent
    if path_key(parent) == path_key(root):
        return ""
    parent_name = normalize_text(parent.name)
    if parent_name.isdigit():
        return ""

    if _is_unknown_or_generic(parent_name):
        grandparent = parent.parent
        if path_key(grandparent) == path_key(root) or _is_unknown_or_generic(grandparent.name):
            return ""
        return normalize_text(grandparent.name)

    if path_key(parent.parent) == path_key(root):
        return parent_name
    if _is_unknown_or_generic(parent.parent.name):
        return ""
    return normalize_text(parent.parent.name)


def _extract_tags(audio: object) -> tuple[dict[str, str], bool, str, bool, str]:
    """Devuelve (tags, tiene_portada, mime, tiene_lyrics, tipo)."""
    tags: dict[str, str] = {}
    has_cover = False
    cover_mime = ""
    has_lyrics = False
    kind = "generic"

    if isinstance(audio, FLAC):
        kind = "vorbis"
        values = _vorbis_map(audio.tags)
        tags = {
            "artist": _first(values.get("artist")),
            "albumartist": _first(values.get("albumartist") or values.get("album artist")),
            "album": _first(values.get("album")),
            "title": _first(values.get("title")),
            "track": _first(values.get("tracknumber") or values.get("track")),
            "disc": _first(values.get("discnumber") or values.get("disc")),
            "date": _first(values.get("date") or values.get("year")),
        }
        has_cover = bool(audio.pictures)
        if audio.pictures:
            front = next((picture for picture in audio.pictures if picture.type == 3), audio.pictures[0])
            cover_mime = front.mime or ""
        has_lyrics = any(values.get(key) for key in ("lyrics", "unsyncedlyrics", "syncedlyrics"))
    elif isinstance(audio, (MP3, AIFF, WAVE)):
        kind = "id3"
        id3 = audio.tags if isinstance(audio.tags, ID3) else None
        tags = {
            "artist": _id3_text(id3, "TPE1"),
            "albumartist": _id3_text(id3, "TPE2"),
            "album": _id3_text(id3, "TALB"),
            "title": _id3_text(id3, "TIT2"),
            "track": _id3_text(id3, "TRCK"),
            "disc": _id3_text(id3, "TPOS"),
            "date": _id3_text(id3, "TDRC") or _id3_text(id3, "TYER"),
        }
        pictures = _id3_pictures(id3)
        has_cover = bool(pictures)
        if pictures:
            front = next((picture for picture in pictures if getattr(picture, "type", 0) == 3), pictures[0])
            cover_mime = front.mime or ""
        has_lyrics = bool(id3 and (id3.getall("USLT") or id3.getall("SYLT")))
    elif isinstance(audio, MP4):
        kind = "mp4"
        tags = {
            "artist": _mp4_text(audio.tags, "\xa9ART"),
            "albumartist": _mp4_text(audio.tags, "aART"),
            "album": _mp4_text(audio.tags, "\xa9alb"),
            "title": _mp4_text(audio.tags, "\xa9nam"),
            "track": _mp4_text(audio.tags, "trkn"),
            "disc": _mp4_text(audio.tags, "disk"),
            "date": _mp4_text(audio.tags, "\xa9day"),
        }
        try:
            covers = audio.tags.get("covr") if audio.tags else None
        except (AttributeError, TypeError):
            covers = None
        has_cover = bool(covers)
        if covers:
            first = covers[0]
            cover_mime = "image/png" if getattr(first, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
        has_lyrics = bool(_mp4_text(audio.tags, "\xa9lyr"))
    elif isinstance(audio, (OggVorbis, OggOpus)):
        kind = "vorbis"
        values = _vorbis_map(audio.tags)
        tags = {
            "artist": _first(values.get("artist")),
            "albumartist": _first(values.get("albumartist") or values.get("album artist")),
            "album": _first(values.get("album")),
            "title": _first(values.get("title")),
            "track": _first(values.get("tracknumber") or values.get("track")),
            "disc": _first(values.get("discnumber") or values.get("disc")),
            "date": _first(values.get("date") or values.get("year")),
        }
        has_cover = _has_vorbis_picture(audio.tags)
        has_lyrics = _has_embedded_lyrics(audio.tags, "vorbis")
    else:
        # Algunos formatos poco habituales exponen tags tipo diccionario.
        tags_obj = getattr(audio, "tags", None)
        tags = {
            "artist": _tag_value(tags_obj, "artist", "TPE1"),
            "albumartist": _tag_value(tags_obj, "albumartist", "album artist", "TPE2"),
            "album": _tag_value(tags_obj, "album", "TALB"),
            "title": _tag_value(tags_obj, "title", "TIT2"),
            "track": _tag_value(tags_obj, "tracknumber", "TRCK"),
            "disc": _tag_value(tags_obj, "discnumber", "TPOS"),
            "date": _tag_value(tags_obj, "date", "TDRC", "TYER"),
        }
        has_cover = _has_vorbis_picture(tags_obj)
        has_lyrics = _has_embedded_lyrics(tags_obj, "vorbis")

    return tags, has_cover, cover_mime, has_lyrics, kind


def read_track(path: Path, root: Path) -> Track:
    audio = mutagen.File(path, easy=False)
    if audio is None:
        raise ValueError("Formato no reconocido por mutagen")
    tags, has_cover, cover_mime, has_lyrics, _kind = _extract_tags(audio)
    info = getattr(audio, "info", None)
    duration = getattr(info, "length", None)
    bitrate = getattr(info, "bitrate", None)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    track_number, _track_total = parse_number_pair(tags.get("track"))
    disc_number, _disc_total = parse_number_pair(tags.get("disc"))

    artist = normalize_text(tags.get("artist"))
    album_artist = normalize_text(tags.get("albumartist"))
    album = normalize_text(tags.get("album"))
    title = normalize_text(tags.get("title"))
    issues: list[str] = []
    inferred: list[str] = []

    if not title:
        title = infer_title_from_filename(path)
        inferred.append("título desde el nombre del archivo")
        issues.append("sin etiqueta de título")
    if not artist and not album_artist:
        inferred_artist = _infer_artist_from_path(path, root)
        if inferred_artist:
            artist = inferred_artist
            inferred.append("artista desde la carpeta")
            issues.append("sin etiqueta de artista")
        else:
            issues.append("sin etiqueta de artista")
    elif not artist:
        artist = album_artist
        inferred.append("artista desde albumartist")
    if not album_artist:
        album_artist = artist
    if not album:
        album = "Unknown Album"
        issues.append("sin etiqueta de álbum")

    return Track(
        path=path,
        artist=artist,
        album_artist=album_artist,
        album=album,
        title=title,
        track_number=track_number,
        disc_number=disc_number,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        format=path.suffix.lower().lstrip("."),
        size=size,
        bitrate=int(bitrate) if isinstance(bitrate, (int, float)) else None,
        has_cover=has_cover,
        cover_mime=cover_mime,
        has_lyrics=has_lyrics,
        issues=issues,
        inferred=inferred,
    )


def _fill_from_library(tracks: list[Track]) -> None:
    known = [
        track for track in tracks
        if track.artist and track.artist != "Unknown Artist"
        and track.album and track.album != "Unknown Album"
    ]
    by_title: dict[str, list[Track]] = defaultdict(list)
    for track in known:
        key = normalize_match(track.title)
        if key and len(key) >= 4:
            by_title[key].append(track)

    for track in tracks:
        candidates = by_title.get(normalize_match(track.title), [])
        if not candidates:
            continue
        if track.duration:
            close = [
                candidate for candidate in candidates
                if candidate.duration and abs(candidate.duration - track.duration) <= 10
            ]
            if close:
                candidates = close
        identities = {
            (candidate.artist.casefold(), candidate.album_artist.casefold(), candidate.album.casefold())
            for candidate in candidates
        }
        if len(identities) != 1:
            continue
        candidate = candidates[0]
        changed = False
        if (not track.artist and not track.album_artist) or track.artist == "Unknown Artist":
            track.artist = candidate.artist
            track.album_artist = candidate.album_artist
            changed = True
            track.inferred.append("artista/álbum desde otra pista de la biblioteca")
        elif not track.album_artist:
            track.album_artist = track.artist
            changed = True
        if (not track.album or track.album == "Unknown Album") and candidate.album:
            track.album = candidate.album
            changed = True
            track.inferred.append("álbum desde otra pista de la biblioteca")
        if changed:
            track.issues = [
                issue for issue in track.issues
                if issue not in {"sin etiqueta de artista", "sin etiqueta de álbum"}
            ]


def _scan_roots(settings: Settings, include_library: bool) -> list[tuple[Path, Path]]:
    """Devuelve pares (archivo, raíz de inferencia), sin rutas duplicadas."""
    source = settings.root
    library = settings.library_root
    same_root = path_key(source) == path_key(library)
    roots: list[tuple[Path, set[Path]]] = [(source, set())]

    if include_library and not same_root:
        source_excludes: set[Path] = set()
        if library.is_relative_to(source):
            # La biblioteca está dentro del origen: se analiza como raíz propia.
            source_excludes.add(library)
        roots[0] = (source, source_excludes)
        roots.append((library, set()))

    found: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for root, excludes in roots:
        if not root.is_dir():
            continue
        for path in iter_audio_files(root, settings.ignore_dirs, excludes):
            key = path_key(path)
            if key in seen:
                continue
            seen.add(key)
            found.append((path, root))
    found.sort(key=lambda pair: str(pair[0]).casefold())
    return found


def scan_library(
    settings: Settings,
    progress: ProgressCallback | None = None,
    *,
    include_library: bool = False,
) -> list[Track]:
    """Lee el origen y, opcionalmente, la biblioteca organizada."""
    entries = _scan_roots(settings, include_library)
    total = len(entries)
    tracks: list[Track] = []
    for index, (path, inference_root) in enumerate(entries, start=1):
        try:
            tracks.append(read_track(path, inference_root))
        except Exception as exc:  # Un archivo dañado no debe detener toda la biblioteca.
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            tracks.append(Track(
                path=path,
                title=infer_title_from_filename(path),
                album="Unknown Album",
                format=path.suffix.lower().lstrip("."),
                size=size,
                issues=[f"error leyendo etiquetas: {exc}"],
            ))
        if progress:
            progress(index, total, str(path))
    _fill_from_library(tracks)
    for track in tracks:
        if not track.artist and not track.album_artist:
            track.artist = "Unknown Artist"
            track.album_artist = "Unknown Artist"
        elif not track.album_artist:
            track.album_artist = track.artist
        if not track.album:
            track.album = "Unknown Album"
    return tracks


def source_tracks(settings: Settings, tracks: Iterable[Track]) -> list[Track]:
    """Filtra las pistas que todavía están en la carpeta de origen."""
    source = settings.root
    library = settings.library_root
    nested_library = (
        path_key(source) != path_key(library)
        and library.is_relative_to(source)
    )
    result: list[Track] = []
    for track in tracks:
        if not track.path.is_relative_to(source):
            continue
        if nested_library and track.path.is_relative_to(library):
            continue
        result.append(track)
    return result


def extract_embedded_lyrics(path: Path) -> tuple[str, bool] | None:
    """Extrae letras incrustadas. Devuelve (texto, es_sincronizada)."""
    try:
        audio = mutagen.File(path, easy=False)
    except Exception:
        return None
    if audio is None:
        return None

    if isinstance(audio, FLAC):
        values = _vorbis_map(audio.tags)
        for key, synced in (("syncedlyrics", True), ("lyrics", False), ("unsyncedlyrics", False)):
            text = _first(values.get(key))
            if text:
                return text, synced or bool(re.search(r"\[\d{1,2}:\d{2}", text))
        return None

    if isinstance(audio, (MP3, AIFF, WAVE)):
        id3 = audio.tags if isinstance(audio.tags, ID3) else None
        if id3 is None:
            return None
        for frame in id3.getall("USLT"):
            if getattr(frame, "text", ""):
                text = str(frame.text)
                return text, bool(re.search(r"\[\d{1,2}:\d{2}", text))
        for frame in id3.getall("SYLT"):
            entries = getattr(frame, "text", None)
            if entries:
                lines = []
                for entry in entries:
                    if isinstance(entry, tuple) and len(entry) >= 2:
                        text, timestamp = entry[0], entry[1]
                        milliseconds = int(timestamp)
                        minutes, remainder = divmod(milliseconds, 60000)
                        seconds, millis = divmod(remainder, 1000)
                        lines.append(f"[{minutes:02d}:{seconds:02d}.{millis // 10:02d}]{text}")
                if lines:
                    return "\n".join(lines), True
        return None

    if isinstance(audio, MP4):
        text = _mp4_text(audio.tags, "\xa9lyr")
        if text:
            return text, bool(re.search(r"\[\d{1,2}:\d{2}", text))
        return None

    values = _vorbis_map(getattr(audio, "tags", None))
    for key, synced in (("syncedlyrics", True), ("lyrics", False), ("unsyncedlyrics", False)):
        text = _first(values.get(key))
        if text:
            return text, synced or bool(re.search(r"\[\d{1,2}:\d{2}", text))
    return None


def scan_overview(tracks: Iterable[Track]) -> dict[str, object]:
    tracks = list(tracks)
    albums = {(track.folder_artist.casefold(), track.album.casefold()) for track in tracks}
    artists = {track.folder_artist.casefold() for track in tracks}
    formats: dict[str, int] = {}
    format_sizes: dict[str, int] = {}
    for track in tracks:
        fmt = track.format or "desconocido"
        formats[fmt] = formats.get(fmt, 0) + 1
        format_sizes[fmt] = format_sizes.get(fmt, 0) + max(0, track.size)
    return {
        "tracks": len(tracks),
        "artists": len(artists),
        "albums": len(albums),
        "with_cover": sum(1 for track in tracks if track.has_cover),
        "with_lyrics": sum(1 for track in tracks if track.has_lyrics),
        "missing_artist": sum(1 for track in tracks if track.artist == "Unknown Artist"),
        "missing_album": sum(1 for track in tracks if track.album == "Unknown Album"),
        "issues": sum(1 for track in tracks if track.issues),
        "duration": sum(track.duration or 0 for track in tracks),
        "total_size": sum(max(0, track.size) for track in tracks),
        "formats": formats,
        "format_sizes": format_sizes,
        "sample_duration": format_duration(sum(track.duration or 0 for track in tracks)),
    }
