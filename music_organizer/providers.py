from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .utils import (
    album_match_score,
    normalize_match,
    text_similarity,
)

USER_AGENT = "OrganizadorMusica/1.0 (herramienta personal para ordenar una biblioteca musical)"


class HttpClient:
    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout
        self._local = threading.local()
        self._rate_lock = threading.Lock()
        self._last_request: dict[str, float] = {}

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "es,en;q=0.8",
            })
            self._local.session = session
        return session

    def _throttle(self, key: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            last = self._last_request.get(key, 0.0)
            wait = min_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_request[key] = now

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        rate_key: str = "",
        min_interval: float = 0.0,
        retries: int = 3,
    ) -> requests.Response | None:
        last_error: Exception | None = None
        for attempt in range(retries):
            self._throttle(rate_key, min_interval)
            try:
                response = self._session().request(
                    method, url, params=params, timeout=self.timeout, allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            if response.status_code == 404:
                return None
            if response.status_code == 429 or 500 <= response.status_code < 600:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * (attempt + 1)
                if attempt + 1 < retries:
                    time.sleep(min(delay, 15))
                    continue
            response.raise_for_status()
            return response
        if last_error:
            raise last_error
        return None

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        response = self.request("GET", url, params=params, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"Respuesta no JSON de {url}") from exc

    def get_bytes(self, url: str, **kwargs: Any) -> tuple[bytes, str] | None:
        response = self.request("GET", url, **kwargs)
        if response is None:
            return None
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        data = response.content
        if not data or len(data) > 15 * 1024 * 1024:
            return None
        return data, content_type


class LrclibClient:
    BASE = "https://lrclib.net/api"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def lookup(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int | None = None,
    ) -> list[dict[str, Any]]:
        if not title:
            return []
        params: dict[str, Any] = {"track_name": title}
        if artist:
            params["artist_name"] = artist
        if album and album != "Unknown Album":
            params["album_name"] = album
        if duration:
            params["duration"] = duration
        exact = self.http.get_json(
            f"{self.BASE}/get", params=params,
            rate_key="lrclib", min_interval=0.28,
        )
        if isinstance(exact, dict) and (exact.get("plainLyrics") or exact.get("syncedLyrics") or exact.get("instrumental")):
            return [exact]

        search_params: dict[str, Any] = {"track_name": title}
        if artist:
            search_params["artist_name"] = artist
        results = self.http.get_json(
            f"{self.BASE}/search", params=search_params,
            rate_key="lrclib", min_interval=0.28,
        )
        if isinstance(results, list) and results:
            return results

        query = f"{artist} {title}".strip()
        results = self.http.get_json(
            f"{self.BASE}/search", params={"q": query},
            rate_key="lrclib", min_interval=0.28,
        )
        return results if isinstance(results, list) else []


class LyricsOvhClient:
    BASE = "https://api.lyrics.ovh/v1"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, artist: str, title: str) -> str | None:
        if not artist or not title:
            return None
        url = f"{self.BASE}/{quote(artist, safe='')}/{quote(title, safe='')}"
        data = self.http.get_json(url, rate_key="lyricsovh", min_interval=1.0)
        if isinstance(data, dict):
            lyrics = str(data.get("lyrics", "")).replace("\r\n", "\n").replace("\r", "\n").strip()
            return lyrics or None
        return None


class ItunesClient:
    BASE = "https://itunes.apple.com/search"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def search_album(self, artist: str, album: str, country: str = "US", limit: int = 8) -> list[dict[str, Any]]:
        term = f"{artist} {album}".strip()
        data = self.http.get_json(
            self.BASE,
            params={
                "term": term,
                "entity": "album",
                "media": "music",
                "limit": limit,
                "country": country,
            },
            rate_key="itunes", min_interval=0.2,
        )
        results = data.get("results", []) if isinstance(data, dict) else []
        return [item for item in results if item.get("wrapperType") == "collection" and item.get("artworkUrl100")]

    @staticmethod
    def artwork_url(item: dict[str, Any], size: int = 1200) -> str:
        url = str(item.get("artworkUrl100", ""))
        return re.sub(r"/\d+x\d+bb\.", f"/{size}x{size}bb.", url)


class DeezerClient:
    BASE = "https://api.deezer.com/search/album"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def search_album(self, artist: str, album: str, limit: int = 10) -> list[dict[str, Any]]:
        query = f"{artist} {album}".strip()
        data = self.http.get_json(
            self.BASE, params={"q": query, "limit": limit},
            rate_key="deezer", min_interval=0.25,
        )
        results = data.get("data", []) if isinstance(data, dict) else []
        return [item for item in results if item.get("cover_xl") or item.get("cover_big")]


class MusicBrainzClient:
    BASE = "https://musicbrainz.org/ws/2/release-group/"
    COVER_BASE = "https://coverartarchive.org/release-group"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def search_release_group(self, artist: str, album: str, limit: int = 5) -> list[dict[str, Any]]:
        escaped_artist = artist.replace('"', "")
        escaped_album = album.replace('"', "")
        query = f'artist:"{escaped_artist}" AND releasegroup:"{escaped_album}"'
        data = self.http.get_json(
            self.BASE,
            params={"query": query, "fmt": "json", "limit": limit},
            rate_key="musicbrainz", min_interval=1.1,
        )
        if not isinstance(data, dict):
            return []
        return [item for item in data.get("release-groups", []) if item.get("id")]

    def cover_bytes(self, release_group_id: str) -> tuple[bytes, str] | None:
        return self.http.get_bytes(
            f"{self.COVER_BASE}/{release_group_id}/front-1200",
            rate_key="coverart", min_interval=0.35,
        )


def is_various_artists(artist: str) -> bool:
    return normalize_match(artist) in {"various artists", "varios artistas", "varios", "vv aa", "v a"}


def score_candidate(
    artist: str,
    album: str,
    candidate_artist: str,
    candidate_album: str,
) -> float:
    various = is_various_artists(artist)
    return album_match_score(artist, album, candidate_artist, candidate_album, various)


def choose_lyrics_candidate(
    candidates: list[dict[str, Any]],
    artist: str,
    title: str,
    album: str,
    duration: int | None,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for candidate in candidates:
        if not (candidate.get("plainLyrics") or candidate.get("syncedLyrics") or candidate.get("instrumental")):
            continue
        title_score = text_similarity(title, candidate.get("trackName", ""))
        artist_score = text_similarity(artist, candidate.get("artistName", "")) if artist else 0.8
        album_score = text_similarity(album, candidate.get("albumName", "")) if album and album != "Unknown Album" else 0.7
        score = title_score * 0.5 + artist_score * 0.3 + album_score * 0.12
        if duration and candidate.get("duration"):
            try:
                difference = abs(float(candidate["duration"]) - duration)
                score += 0.08 * max(0.0, 1.0 - difference / 30.0)
            except (TypeError, ValueError):
                pass
        if candidate.get("syncedLyrics"):
            score += 0.02
        if title_score < 0.58:
            continue
        if artist and artist != "Unknown Artist" and artist_score < 0.38:
            continue
        if score > best_score:
            best_score = score
            best = candidate
    return best if best_score >= 0.55 else None


def cache_key(*parts: object) -> str:
    payload = "\x1f".join(normalize_match(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JsonCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> Any:
        path = self.path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        temp = path.with_suffix(".part")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        temp.replace(path)


def image_extension(data: bytes, content_type: str = "") -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"GIF8"):
        return ".gif"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    return ""


def decode_vorbis_picture(value: str) -> tuple[bytes, str] | None:
    try:
        from mutagen.flac import Picture
        raw = base64.b64decode(value)
        picture = Picture(raw)
        return bytes(picture.data), picture.mime or ""
    except Exception:
        return None
