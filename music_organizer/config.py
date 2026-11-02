from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .utils import AUDIO_EXTENSIONS

APP_DIR = Path(__file__).resolve().parent.parent


@dataclass(slots=True)
class Settings:
    root: Path
    library_root: Path | None = None
    state_root: Path | None = None
    artist_field: str = "albumartist"
    conflict: str = "ask"
    clean_empty_dirs: bool = False
    overwrite_lyrics: bool = False
    overwrite_covers: bool = False
    lyrics_provider: str = "both"
    country: str = "US"
    workers: int = 3
    timeout: int = 20
    max_path: int = 240
    state_dirname: str = ".music-organizer"
    ignore_dirnames: tuple[str, ...] = (
        ".music-organizer", ".thumbnails", "$RECYCLE.BIN", "System Volume Information",
    )
    audio_extensions: frozenset[str] = field(default_factory=lambda: frozenset(AUDIO_EXTENSIONS))

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.library_root = Path(self.library_root or self.root).expanduser().resolve()
        self.state_root = Path(self.state_root or APP_DIR).expanduser().resolve()
        if self.artist_field not in {"albumartist", "artist"}:
            self.artist_field = "albumartist"
        if self.conflict == "rename":
            self.conflict = "keep"
        if self.conflict not in {"ask", "overwrite", "keep", "skip"}:
            self.conflict = "ask"
        if self.lyrics_provider not in {"lrclib", "lyricsovh", "both"}:
            self.lyrics_provider = "both"
        self.workers = max(1, min(int(self.workers), 8))
        self.timeout = max(5, min(int(self.timeout), 120))

    @property
    def source_root(self) -> Path:
        return self.root

    @property
    def state_dir(self) -> Path:
        return self.state_root / self.state_dirname

    @property
    def journal_dir(self) -> Path:
        return self.state_dir / "journal"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def report_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def ignore_dirs(self) -> set[Path]:
        roots = {self.root, self.library_root}
        return {
            root / name
            for root in roots
            for name in self.ignore_dirnames
        }

    def ensure_state_dirs(self) -> None:
        for path in (self.state_dir, self.journal_dir, self.cache_dir, self.report_dir):
            path.mkdir(parents=True, exist_ok=True)
