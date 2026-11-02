from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .utils import path_key


@dataclass(slots=True)
class MediaInfo:
    path: Path
    format: str
    size: int = 0
    duration: float | None = None
    bitrate: int | None = None

    @property
    def format_label(self) -> str:
        return (self.format or self.path.suffix.lstrip(".")).upper()


@dataclass(slots=True)
class Track:
    path: Path
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    title: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    duration: float | None = None
    format: str = ""
    size: int = 0
    bitrate: int | None = None
    has_cover: bool = False
    cover_mime: str = ""
    has_lyrics: bool = False
    issues: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)

    @property
    def folder_artist(self) -> str:
        return self.album_artist or self.artist or "Unknown Artist"

    @property
    def display_artist(self) -> str:
        return self.artist or self.album_artist or "Unknown Artist"

    @property
    def duration_int(self) -> int | None:
        if self.duration and self.duration > 0:
            return int(round(self.duration))
        return None

    @property
    def album_key(self) -> tuple[str, str]:
        return (self.folder_artist.casefold(), self.album.casefold())


@dataclass(slots=True)
class PlanItem:
    kind: str
    status: str
    source: Path
    target: Path
    track: Track | None = None
    reason: str = ""
    alternate_target: Path | None = None
    source_info: MediaInfo | None = None
    target_info: MediaInfo | None = None
    audio_source: Path | None = None

    @property
    def will_change(self) -> bool:
        return self.status in {"move", "rename", "conflict"}


@dataclass(slots=True)
class OrganizePlan:
    root: Path
    library_root: Path
    items: list[PlanItem] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    target_for_track: dict[Path, Path] = field(default_factory=dict)

    @property
    def audio_items(self) -> list[PlanItem]:
        return [item for item in self.items if item.kind == "audio"]

    @property
    def companion_items(self) -> list[PlanItem]:
        return [item for item in self.items if item.kind == "companion"]

    @property
    def changes(self) -> list[PlanItem]:
        return [item for item in self.items if item.will_change]

    @property
    def conflicts(self) -> list[PlanItem]:
        return [item for item in self.audio_items if item.status == "conflict"]

    @property
    def organized_matches(self) -> int:
        """Pistas cuyo padre ya coincide con Artista/Álbum en el origen."""
        matches = 0
        for item in self.audio_items:
            if item.status == "missing":
                continue
            try:
                source_parent = item.source.parent.relative_to(self.root)
                target_parent = item.target.parent.relative_to(self.library_root)
            except ValueError:
                continue
            if path_key(source_parent) == path_key(target_parent):
                matches += 1
        return matches

    @property
    def already_organized(self) -> bool:
        audio = [item for item in self.audio_items if item.status != "missing"]
        return bool(audio) and self.organized_matches == len(audio)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.items:
            result[item.status] = result.get(item.status, 0) + 1
        return result

    @property
    def albums(self) -> int:
        return len({(track.folder_artist.casefold(), track.album.casefold()) for track in self.tracks})


@dataclass(slots=True)
class LyricsOutcome:
    track: Track
    status: str
    path: Path | None = None
    provider: str = ""
    message: str = ""
    synced: bool = False


@dataclass(slots=True)
class CoverOutcome:
    folder: Path
    status: str
    path: Path | None = None
    provider: str = ""
    message: str = ""
    tracks: int = 0


@dataclass(slots=True)
class OperationSummary:
    operation: str
    total: int = 0
    changed: int = 0
    skipped: int = 0
    errors: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    journal_path: Path | None = None
    report_path: Path | None = None

    def add(self, status: str, count: int = 1) -> None:
        self.counts[status] = self.counts.get(status, 0) + count
        self.total += count

    def line(self) -> str:
        parts = [f"{key}: {value}" for key, value in sorted(self.counts.items())]
        return ", ".join(parts) if parts else "sin cambios"
