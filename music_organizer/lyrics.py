from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .journal import RunJournal, start_run
from .models import LyricsOutcome, OperationSummary, Track
from .providers import (
    HttpClient,
    JsonCache,
    LrclibClient,
    LyricsOvhClient,
    cache_key,
    choose_lyrics_candidate,
)
from .scanner import extract_embedded_lyrics
from .utils import atomic_write_text, format_duration, normalize_text, sha256_bytes

ProgressCallback = Callable[[int, int, str], None]


def _safe_header(value: object | None) -> str:
    return normalize_text(value).replace("[", "(").replace("]", ")")


def _has_header(text: str, key: str) -> bool:
    return re.search(rf"^\s*\[{re.escape(key)}:", text, flags=re.IGNORECASE | re.MULTILINE) is not None


def format_lrc(track: Track, text: str, provider: str, synced: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    if not text:
        return ""
    if not synced and re.search(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]", text):
        synced = True
    headers: list[str] = []
    if not _has_header(text, "ti"):
        headers.append(f"[ti:{_safe_header(track.title)}]")
    if not _has_header(text, "ar"):
        headers.append(f"[ar:{_safe_header(track.artist or track.album_artist)}]")
    if not _has_header(text, "al") and track.album and track.album != "Unknown Album":
        headers.append(f"[al:{_safe_header(track.album)}]")
    if not _has_header(text, "length") and track.duration_int:
        headers.append(f"[length:{format_duration(track.duration_int)}]")
    if not _has_header(text, "by"):
        headers.append(f"[by:{provider}]")
    if headers:
        prefix = "\n".join(headers)
        if synced:
            text = f"{prefix}\n{text}"
        else:
            text = f"{prefix}\n\n{text}"
    return text.rstrip() + "\n"


@dataclass
class _FetchedLyrics:
    provider: str
    text: str
    synced: bool
    instrumental: bool = False


class LyricsService:
    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient(settings.timeout)
        self.lrclib = LrclibClient(self.http)
        self.ovh = LyricsOvhClient(self.http)
        self.cache = JsonCache(settings.cache_dir / "lyrics")
        self._cache_lock = threading.Lock()

    @staticmethod
    def target_for(track: Track) -> Path:
        return track.path.with_suffix(".lrc")

    def inspect(self, track: Track) -> str:
        target = self.target_for(track)
        if target.exists() and target.stat().st_size > 0:
            return "exists"
        if track.has_lyrics:
            return "would_extract"
        return "would_fetch"

    def _cached(self, key: str) -> dict | None:
        with self._cache_lock:
            value = self.cache.get(key)
        if not isinstance(value, dict):
            return None
        if value.get("not_found"):
            created = value.get("at", 0)
            if isinstance(created, (int, float)) and time.time() - created < 30 * 86400:
                return value
            return None
        return value

    def _store_cache(self, key: str, value: dict) -> None:
        value = dict(value)
        value["at"] = time.time()
        with self._cache_lock:
            self.cache.set(key, value)

    def _fetch_online(self, track: Track) -> _FetchedLyrics | None:
        artist = "" if track.artist == "Unknown Artist" else (track.artist or track.album_artist)
        album = "" if track.album == "Unknown Album" else track.album
        key = cache_key("lyrics-v1", artist, track.title, album, track.duration_int)
        cached = self._cached(key)
        if cached:
            if cached.get("not_found"):
                return None
            text = cached.get("text")
            if isinstance(text, str) and text.strip():
                return _FetchedLyrics(
                    provider=str(cached.get("provider", "caché")),
                    text=text,
                    synced=bool(cached.get("synced")),
                    instrumental=bool(cached.get("instrumental")),
                )
            return None

        result: _FetchedLyrics | None = None
        last_error = ""
        if self.settings.lyrics_provider in {"lrclib", "both"}:
            try:
                candidates = self.lrclib.lookup(artist, track.title, album, track.duration_int)
                chosen = choose_lyrics_candidate(candidates, artist, track.title, album, track.duration_int)
                if chosen:
                    if chosen.get("instrumental"):
                        result = _FetchedLyrics(provider="LRCLIB", text="", synced=False, instrumental=True)
                    else:
                        synced_text = chosen.get("syncedLyrics") or ""
                        plain_text = chosen.get("plainLyrics") or ""
                        if synced_text:
                            result = _FetchedLyrics(provider="LRCLIB", text=str(synced_text), synced=True)
                        elif plain_text:
                            result = _FetchedLyrics(provider="LRCLIB", text=str(plain_text), synced=False)
            except Exception as exc:
                last_error = f"LRCLIB: {exc}"
        if result is None and self.settings.lyrics_provider in {"lyricsovh", "both"} and artist:
            try:
                text = self.ovh.fetch(artist, track.title)
                if text:
                    result = _FetchedLyrics(provider="Lyrics.ovh", text=text, synced=False)
            except Exception as exc:
                last_error = f"{last_error}; Lyrics.ovh: {exc}" if last_error else f"Lyrics.ovh: {exc}"

        if result is None:
            if last_error:
                raise RuntimeError(last_error)
            self._store_cache(key, {"not_found": True})
        elif result.instrumental:
            self._store_cache(key, {
                "instrumental": True, "provider": result.provider,
                "text": "", "synced": False,
            })
        else:
            self._store_cache(key, {
                "text": result.text, "synced": result.synced,
                "provider": result.provider, "instrumental": False,
            })
        return result

    def fetch_one(
        self,
        track: Track,
        journal: RunJournal | None = None,
        apply: bool = True,
        overwrite: bool | None = None,
    ) -> LyricsOutcome:
        overwrite = self.settings.overwrite_lyrics if overwrite is None else overwrite
        target = self.target_for(track)
        if target.exists() and target.stat().st_size > 0 and not overwrite:
            return LyricsOutcome(track=track, status="exists", path=target, provider="archivo local")
        if not apply:
            status = self.inspect(track)
            return LyricsOutcome(track=track, status=status, path=target, provider="")

        embedded = None
        if track.has_lyrics:
            embedded = extract_embedded_lyrics(track.path)
        if embedded:
            text, synced = embedded
            content = format_lrc(track, text, "etiqueta incrustada", synced)
            if content:
                return self._write(track, target, content, "etiqueta incrustada", synced, journal, overwrite)

        try:
            fetched = self._fetch_online(track)
        except Exception as exc:
            return LyricsOutcome(
                track=track, status="error", path=target, provider="",
                message=f"error de red: {exc}",
            )
        if fetched is None:
            return LyricsOutcome(
                track=track, status="not_found", path=target, provider="",
                message="no se encontraron letras",
            )
        if fetched.instrumental:
            return LyricsOutcome(
                track=track, status="instrumental", path=target,
                provider=fetched.provider, message="la canción figura como instrumental",
            )
        content = format_lrc(track, fetched.text, fetched.provider, fetched.synced)
        if not content:
            return LyricsOutcome(
                track=track, status="not_found", path=target, provider=fetched.provider,
                message="letras vacías",
            )
        return self._write(track, target, content, fetched.provider, fetched.synced, journal, overwrite)

    def _write(
        self,
        track: Track,
        target: Path,
        content: str,
        provider: str,
        synced: bool,
        journal: RunJournal | None,
        overwrite: bool,
    ) -> LyricsOutcome:
        if target.exists() and target.stat().st_size > 0 and not overwrite:
            return LyricsOutcome(track=track, status="exists", path=target, provider=provider)
        if journal and target.exists() and target.stat().st_size > 0:
            journal.backup_file(target)
        atomic_write_text(target, content, encoding="utf-8")
        if journal:
            journal.log_created(target, sha256_bytes(content.encode("utf-8")))
        return LyricsOutcome(
            track=track, status="downloaded", path=target, provider=provider, synced=synced,
        )

    def run(
        self,
        tracks: list[Track],
        apply: bool = True,
        overwrite: bool | None = None,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> OperationSummary:
        summary = OperationSummary(operation="lyrics")
        selected = tracks[:limit] if limit else tracks
        total = len(selected)
        overwrite = self.settings.overwrite_lyrics if overwrite is None else overwrite
        if not apply:
            for index, track in enumerate(selected, start=1):
                status = self.inspect(track)
                summary.add(status)
                if status == "would_fetch":
                    summary.details.append(f"Faltan letras: {track.display_artist} - {track.title}")
                if progress:
                    progress(index, total, f"{track.display_artist} - {track.title}")
            summary.changed = summary.counts.get("would_fetch", 0) + summary.counts.get("would_extract", 0)
            return summary

        self.settings.ensure_state_dirs()
        journal = start_run(self.settings, "lyrics")
        summary.journal_path = journal.path
        outcomes: list[LyricsOutcome] = []
        done = 0

        with ThreadPoolExecutor(max_workers=self.settings.workers) as executor:
            futures = {
                executor.submit(self.fetch_one, track, journal, True, overwrite): track
                for track in selected
            }
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                track = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = LyricsOutcome(
                        track=track, status="error", path=self.target_for(track),
                        message=f"error inesperado: {exc}",
                    )
                outcomes.append(outcome)
                summary.add(outcome.status)
                if outcome.status in {"downloaded"}:
                    summary.changed += 1
                elif outcome.status == "error":
                    summary.errors += 1
                    if len(summary.details) < 80:
                        summary.details.append(f"{track.display_artist} - {track.title}: {outcome.message}")
                elif outcome.status == "not_found":
                    if len(summary.details) < 80:
                        summary.details.append(f"Sin letras: {track.display_artist} - {track.title}")
                done += 1
                if progress:
                    progress(done, total, f"{track.display_artist} - {track.title}")

        summary.skipped = sum(
            count for status, count in summary.counts.items()
            if status in {"exists", "instrumental", "not_found"}
        )
        return summary


def summarize_outcomes(outcomes: list[LyricsOutcome]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
