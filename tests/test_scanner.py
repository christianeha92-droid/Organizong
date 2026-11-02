from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music_organizer.config import Settings
from music_organizer.models import Track
from music_organizer.scanner import scan_library, source_tracks


def fake_track(path: Path, root: Path) -> Track:
    return Track(
        path=path,
        artist="Artist",
        album_artist="Artist",
        album="Album",
        title=path.stem,
        format=path.suffix.lower().lstrip("."),
        size=path.stat().st_size,
    )


class ScannerRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.library = self.base / "library"
        self.settings = Settings(
            root=self.source,
            library_root=self.library,
            state_root=self.base / "state",
        )
        for path in (
            self.source / "incoming" / "one.flac",
            self.library / "Artist" / "Album" / "02 - Two.flac",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scan_can_include_the_organized_library(self) -> None:
        with patch("music_organizer.scanner.read_track", side_effect=fake_track):
            source_only = scan_library(self.settings)
            everything = scan_library(self.settings, include_library=True)

        self.assertEqual(len(source_only), 1)
        self.assertEqual(source_only[0].path.name, "one.flac")
        self.assertEqual(len(everything), 2)
        self.assertEqual(
            {track.path.name for track in everything}, {"one.flac", "02 - Two.flac"},
        )

    def test_source_tracks_excludes_the_library_when_nested(self) -> None:
        nested_library = self.source / "Organizada"
        path = nested_library / "Artist" / "Album" / "03 - Three.flac"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"audio")
        settings = Settings(
            root=self.source,
            library_root=nested_library,
            state_root=self.base / "state",
        )
        tracks = [
            Track(path=self.source / "incoming" / "one.flac"),
            Track(path=path),
        ]

        selected = source_tracks(settings, tracks)

        self.assertEqual([track.path.name for track in selected], ["one.flac"])

    def test_scan_does_not_duplicate_files_when_roots_overlap(self) -> None:
        settings = Settings(
            root=self.source,
            library_root=self.base,
            state_root=self.base / "state",
        )
        with patch("music_organizer.scanner.read_track", side_effect=fake_track):
            tracks = scan_library(settings, include_library=True)

        keys = [str(track.path).casefold() for track in tracks]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(tracks), 2)


if __name__ == "__main__":
    unittest.main()
