from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from .config import Settings
from .journal import RunJournal, start_run
from .models import CoverOutcome, OperationSummary, Track
from .providers import (
    DeezerClient,
    HttpClient,
    ItunesClient,
    MusicBrainzClient,
    cache_key,
    decode_vorbis_picture,
    image_extension,
    is_various_artists,
    score_candidate,
)
from .utils import (
    IMAGE_EXTENSIONS,
    album_similarity,
    artist_similarity,
    atomic_write_bytes,
    normalize_match,
    path_key,
    sha256_bytes,
)

ProgressCallback = Callable[[int, int, str], None]

_COVER_STEMS = {
    "cover", "folder", "front", "album", "albumart", "album art", "caratula", "portada",
}


@dataclass(slots=True)
class CoverImage:
    data: bytes
    mime: str
    extension: str


def _image_from_bytes(data: bytes, mime: str = "") -> CoverImage | None:
    if not data:
        return None
    extension = image_extension(data, mime)
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    return CoverImage(
        data=data,
        mime=mime or ("image/png" if extension == ".png" else "image/jpeg"),
        extension=".jpg" if extension == ".jpeg" else extension,
    )


def extract_embedded_cover(path: Path) -> CoverImage | None:
    try:
        audio = mutagen.File(path, easy=False)
    except Exception:
        return None
    if audio is None:
        return None

    if isinstance(audio, FLAC):
        if not audio.pictures:
            return None
        picture = next((item for item in audio.pictures if item.type == 3), audio.pictures[0])
        return _image_from_bytes(bytes(picture.data), picture.mime or "")

    if isinstance(audio, (MP3, AIFF, WAVE)):
        id3 = audio.tags if isinstance(audio.tags, ID3) else None
        if id3 is None:
            return None
        pictures = list(id3.getall("APIC"))
        if not pictures:
            return None
        picture = next((item for item in pictures if getattr(item, "type", 0) == 3), pictures[0])
        return _image_from_bytes(bytes(picture.data), picture.mime or "")

    if isinstance(audio, MP4):
        try:
            covers = audio.tags.get("covr") if audio.tags else None
        except (AttributeError, TypeError):
            covers = None
        if not covers:
            return None
        first = covers[0]
        mime = "image/png" if getattr(first, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
        return _image_from_bytes(bytes(first), mime)

    if isinstance(audio, (OggVorbis, OggOpus)):
        tags = getattr(audio, "tags", None)
        if tags is None:
            return None
        values = []
        for key, value in tags.items():
            if str(key).casefold() == "metadata_block_picture":
                values = value if isinstance(value, list) else [value]
                break
        if not values:
            return None
        decoded = decode_vorbis_picture(str(values[0]))
        if decoded:
            return _image_from_bytes(decoded[0], decoded[1])
    return None


def find_existing_cover(folder: Path) -> Path | None:
    if not folder.exists():
        return None
    candidates: list[Path] = []
    try:
        for item in folder.iterdir():
            if not item.is_file() or item.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if normalize_match(item.stem) in _COVER_STEMS:
                candidates.append(item)
    except OSError:
        return None
    if not candidates:
        return None
    priority = {"cover": 0, "folder": 1, "front": 2, "album": 3, "albumart": 4}
    return sorted(candidates, key=lambda item: (priority.get(normalize_match(item.stem), 9), item.name.casefold()))[0]


def _group_tracks(tracks: list[Track]) -> list[list[Track]]:
    groups: dict[tuple[str, str, str], list[Track]] = {}
    for track in tracks:
        key = (
            path_key(track.path.parent),
            normalize_match(track.album),
            normalize_match(track.album_artist or track.artist),
        )
        groups.setdefault(key, []).append(track)
    return sorted(groups.values(), key=lambda group: str(group[0].path.parent).casefold())


class CoverService:
    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient(settings.timeout)
        self.itunes = ItunesClient(self.http)
        self.deezer = DeezerClient(self.http)
        self.musicbrainz = MusicBrainzClient(self.http)
        self.cache_dir = settings.cache_dir / "covers"
        self._cache_lock = threading.Lock()

    def inspect(self, tracks: list[Track]) -> str:
        if not tracks:
            return "not_found"
        folder = tracks[0].path.parent
        if find_existing_cover(folder):
            return "exists"
        if any(track.has_cover for track in tracks):
            return "would_extract"
        return "would_fetch"

    def _cache_meta_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _load_cache(self, key: str) -> CoverImage | str | None:
        meta_path = self._cache_meta_path(key)
        if not meta_path.exists():
            return None
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            return None
        if meta.get("not_found"):
            created = meta.get("at", 0)
            if isinstance(created, (int, float)) and time.time() - created < 30 * 86400:
                return "not_found"
            return None
        extension = str(meta.get("extension", ".jpg"))
        image_path = self.cache_dir / f"{key}{extension}"
        if not image_path.exists():
            return None
        try:
            data = image_path.read_bytes()
        except OSError:
            return None
        image = _image_from_bytes(data, str(meta.get("mime", "")))
        if image:
            image.mime = str(meta.get("mime", image.mime))
        return image

    def _store_cache(self, key: str, image: CoverImage | None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if image is None:
            meta = {"not_found": True, "at": time.time()}
            temp = self._cache_meta_path(key).with_suffix(".part")
            temp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._cache_meta_path(key))
            return
        image_path = self.cache_dir / f"{key}{image.extension}"
        atomic_write_bytes(image_path, image.data)
        meta = {
            "extension": image.extension,
            "mime": image.mime,
            "at": time.time(),
            "provider": getattr(image, "provider", ""),
        }
        temp = self._cache_meta_path(key).with_suffix(".part")
        temp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        temp.replace(self._cache_meta_path(key))

    @staticmethod
    def _accept_candidate(artist: str, album: str, candidate_artist: str, candidate_album: str) -> bool:
        if not album or album == "Unknown Album":
            return False
        album_score = album_similarity(album, candidate_album)
        if album_score < 0.58:
            return False
        various = is_various_artists(artist)
        if artist and artist != "Unknown Artist" and not various:
            if artist_similarity(artist, candidate_artist) < 0.4:
                return False
        return score_candidate(artist, album, candidate_artist, candidate_album) >= 0.62

    def _download_image(self, url: str, rate_key: str, min_interval: float) -> CoverImage | None:
        try:
            result = self.http.get_bytes(url, rate_key=rate_key, min_interval=min_interval)
        except Exception:
            return None
        if not result:
            return None
        data, content_type = result
        return _image_from_bytes(data, content_type)

    def _fetch_online(self, artist: str, album: str) -> tuple[CoverImage | None, str]:
        if not album or album == "Unknown Album":
            return None, "sin datos de álbum"
        key = cache_key("cover-v1", artist, album)
        cached = self._load_cache(key)
        if cached == "not_found":
            return None, "sin resultados (caché)"
        if isinstance(cached, CoverImage):
            return cached, "caché"

        # 1) iTunes
        try:
            candidates = self.itunes.search_album(artist, album, self.settings.country)
        except Exception:
            candidates = []
        scored: list[tuple[float, dict]] = []
        for candidate in candidates:
            cand_artist = str(candidate.get("artistName", ""))
            cand_album = str(candidate.get("collectionName", ""))
            if self._accept_candidate(artist, album, cand_artist, cand_album):
                scored.append((score_candidate(artist, album, cand_artist, cand_album), candidate))
        for _score, candidate in sorted(scored, key=lambda item: item[0], reverse=True):
            image = self._download_image(
                self.itunes.artwork_url(candidate), "itunes-art", 0.1,
            )
            if image:
                self._store_cache(key, image)
                return image, "iTunes"

        # 2) Deezer
        try:
            candidates = self.deezer.search_album(artist, album)
        except Exception:
            candidates = []
        scored = []
        for candidate in candidates:
            cand_artist = str((candidate.get("artist") or {}).get("name", ""))
            cand_album = str(candidate.get("title", ""))
            if self._accept_candidate(artist, album, cand_artist, cand_album):
                scored.append((score_candidate(artist, album, cand_artist, cand_album), candidate))
        for _score, candidate in sorted(scored, key=lambda item: item[0], reverse=True):
            url = str(candidate.get("cover_xl") or candidate.get("cover_big") or "")
            if not url:
                continue
            image = self._download_image(url, "deezer-art", 0.1)
            if image:
                self._store_cache(key, image)
                return image, "Deezer"

        # 3) MusicBrainz + Cover Art Archive
        try:
            candidates = self.musicbrainz.search_release_group(artist, album)
        except Exception:
            candidates = []
        scored = []
        for candidate in candidates:
            credits = candidate.get("artist-credit") or []
            cand_artist = ", ".join(
                str(item.get("name", "")) for item in credits if isinstance(item, dict)
            )
            cand_album = str(candidate.get("title", ""))
            if self._accept_candidate(artist, album, cand_artist, cand_album):
                scored.append((score_candidate(artist, album, cand_artist, cand_album), candidate))
        for _score, candidate in sorted(scored, key=lambda item: item[0], reverse=True):
            result = None
            try:
                result = self.musicbrainz.cover_bytes(str(candidate.get("id")))
            except Exception:
                result = None
            if result:
                image = _image_from_bytes(result[0], result[1])
                if image:
                    self._store_cache(key, image)
                    return image, "MusicBrainz / Cover Art Archive"

        self._store_cache(key, None)
        return None, "sin resultados"

    def _write_cover(
        self,
        folder: Path,
        image: CoverImage,
        provider: str,
        journal: RunJournal,
        overwrite: bool,
        track_count: int = 0,
    ) -> CoverOutcome:
        target = folder / f"cover{image.extension}"
        if target.exists() and not overwrite:
            return CoverOutcome(
                folder=folder, status="exists", path=target, provider=provider,
                message="la portada ya existe", tracks=track_count,
            )
        if target.exists() and overwrite:
            journal.backup_file(target)
        atomic_write_bytes(target, image.data)
        journal.log_created(target, sha256_bytes(image.data))
        return CoverOutcome(
            folder=folder, status="downloaded", path=target, provider=provider,
            tracks=track_count,
        )

    def _process_group(
        self,
        group: list[Track],
        journal: RunJournal,
        overwrite: bool,
    ) -> CoverOutcome:
        folder = group[0].path.parent
        existing = find_existing_cover(folder)
        if existing and not overwrite:
            return CoverOutcome(
                folder=folder, status="exists", path=existing, provider="archivo local",
                tracks=len(group),
            )
        for track in group:
            if not track.has_cover:
                continue
            image = extract_embedded_cover(track.path)
            if image:
                return self._write_cover(
                    folder, image, "portada incrustada", journal, overwrite, len(group),
                )
        representative = next(
            (track for track in group if track.album_artist and track.album_artist != "Unknown Artist"),
            group[0],
        )
        artist = representative.album_artist or representative.artist
        album = representative.album
        image, provider = self._fetch_online(artist, album)
        if image is None:
            return CoverOutcome(
                folder=folder, status="not_found", provider="",
                message=provider, tracks=len(group),
            )
        return self._write_cover(folder, image, provider, journal, overwrite, len(group))

    def run(
        self,
        tracks: list[Track],
        apply: bool = True,
        overwrite: bool | None = None,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> OperationSummary:
        summary = OperationSummary(operation="covers")
        groups = _group_tracks(tracks)
        if limit:
            groups = groups[:limit]
        total = len(groups)
        overwrite = self.settings.overwrite_covers if overwrite is None else overwrite

        if not apply:
            for index, group in enumerate(groups, start=1):
                status = self.inspect(group)
                summary.add(status)
                if status in {"would_fetch", "would_extract"}:
                    track = group[0]
                    summary.details.append(f"Falta portada: {track.folder_artist} - {track.album}")
                if progress:
                    progress(index, total, str(group[0].path.parent))
            summary.changed = summary.counts.get("would_fetch", 0) + summary.counts.get("would_extract", 0)
            return summary

        self.settings.ensure_state_dirs()
        journal = start_run(self.settings, "covers")
        summary.journal_path = journal.path
        done = 0
        for group in groups:
            if stop_event and stop_event.is_set():
                break
            try:
                outcome = self._process_group(group, journal, overwrite)
            except Exception as exc:
                outcome = CoverOutcome(
                    folder=group[0].path.parent, status="error",
                    message=f"error inesperado: {exc}", tracks=len(group),
                )
            summary.add(outcome.status)
            if outcome.status == "downloaded":
                summary.changed += 1
            elif outcome.status == "error":
                summary.errors += 1
            elif outcome.status == "not_found":
                if len(summary.details) < 80:
                    summary.details.append(
                        f"Sin portada: {group[0].folder_artist} - {group[0].album} ({outcome.message})"
                    )
            done += 1
            if progress:
                progress(done, total, str(group[0].path.parent))
        summary.skipped = sum(
            count for status, count in summary.counts.items()
            if status in {"exists", "not_found"}
        )
        return summary
