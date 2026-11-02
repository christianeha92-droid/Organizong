from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_organizer.config import Settings
from music_organizer.models import Track
from music_organizer.operations import apply_plan, undo_last
from music_organizer.planner import build_plan
from music_organizer.utils import path_key


def make_track(path: Path, *, artist: str = "Artist", album: str = "Album", title: str = "Song") -> Track:
    return Track(
        path=path,
        artist=artist,
        album_artist=artist,
        album=album,
        title=title,
        track_number=1,
        duration=180.0,
        format=path.suffix.lower().lstrip("."),
        size=path.stat().st_size if path.exists() else 0,
    )


class OperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.library = self.base / "library"
        self.state = self.base / "state"
        self.settings = Settings(
            root=self.source,
            library_root=self.library,
            state_root=self.state,
        )
        self.source.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def conflict_plan(self, *, companion: bool = False):
        audio = self.write(self.source / "incoming" / "song.flac", b"new-audio")
        if companion:
            self.write(self.source / "incoming" / "song.lrc", b"new-lyrics")
        existing = self.write(
            self.library / "Artist" / "Album" / "01 - Song.flac", b"old-audio",
        )
        if companion:
            self.write(
                self.library / "Artist" / "Album" / "01 - Song.lrc", b"old-lyrics",
            )
        plan = build_plan(self.settings, [make_track(audio, title="Song")])
        return plan, audio, existing

    def test_overwrite_moves_audio_and_undo_restores_both_versions(self) -> None:
        plan, source, target = self.conflict_plan()
        decisions = {path_key(source): "overwrite"}

        summary = apply_plan(plan, self.settings, decisions=decisions)

        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.changed, 1)
        self.assertEqual(summary.counts, {"sobrescrito": 1})
        self.assertEqual(target.read_bytes(), b"new-audio")
        self.assertFalse(source.exists())
        backups = list(self.settings.backup_dir.rglob("01 - Song.flac"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old-audio")

        undone = undo_last(self.settings)

        self.assertEqual(undone.errors, 0)
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), b"new-audio")
        self.assertEqual(target.read_bytes(), b"old-audio")

    def test_keep_saves_audio_in_alternate_album(self) -> None:
        plan, source, target = self.conflict_plan()
        alternate = plan.audio_items[0].alternate_target
        self.assertIsNotNone(alternate)
        decisions = {path_key(source): "keep"}

        summary = apply_plan(plan, self.settings, decisions=decisions)

        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.counts, {"guardado_aparte": 1})
        self.assertEqual(target.read_bytes(), b"old-audio")
        self.assertEqual(alternate.read_bytes(), b"new-audio")
        self.assertFalse(source.exists())

        undone = undo_last(self.settings)

        self.assertEqual(undone.errors, 0)
        self.assertTrue(source.exists())
        self.assertFalse(alternate.exists())
        self.assertEqual(target.read_bytes(), b"old-audio")

    def test_skip_leaves_audio_untouched(self) -> None:
        plan, source, target = self.conflict_plan()
        decisions = {path_key(source): "skip"}

        summary = apply_plan(plan, self.settings, decisions=decisions)

        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.counts, {"omitido": 1})
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b"old-audio")

    def test_conflict_decision_applies_to_lyrics_companion(self) -> None:
        plan, source, target = self.conflict_plan(companion=True)
        alternate = plan.audio_items[0].alternate_target
        decisions = {path_key(source): "keep"}

        summary = apply_plan(plan, self.settings, decisions=decisions)

        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.counts.get("guardado_aparte"), 2)
        self.assertEqual(target.read_bytes(), b"old-audio")
        self.assertEqual(alternate.read_bytes(), b"new-audio")
        alternate_lyrics = alternate.with_suffix(".lrc")
        self.assertEqual(alternate_lyrics.read_bytes(), b"new-lyrics")
        self.assertFalse((self.source / "incoming" / "song.lrc").exists())

    def test_companion_conflict_is_left_when_audio_has_no_conflict(self) -> None:
        audio = self.write(self.source / "incoming" / "song.flac", b"new-audio")
        self.write(self.source / "incoming" / "song.lrc", b"new-lyrics")
        self.write(
            self.library / "Artist" / "Album" / "01 - Song.lrc", b"old-lyrics",
        )
        plan = build_plan(self.settings, [make_track(audio, title="Song")])

        summary = apply_plan(plan, self.settings)

        self.assertEqual(summary.errors, 0)
        self.assertTrue((self.library / "Artist" / "Album" / "01 - Song.flac").exists())
        self.assertEqual(
            (self.library / "Artist" / "Album" / "01 - Song.lrc").read_bytes(),
            b"old-lyrics",
        )
        self.assertTrue((self.source / "incoming" / "song.lrc").exists())
        self.assertEqual(summary.counts.get("omitido"), 1)

    def test_undo_does_not_touch_a_different_run(self) -> None:
        first_plan, first_source, _target = self.conflict_plan()
        apply_plan(
            first_plan, self.settings,
            decisions={path_key(first_source): "overwrite"},
        )
        undo_last(self.settings)

        second_source = self.write(
            self.source / "incoming" / "other.flac", b"other-audio",
        )
        second_plan = build_plan(
            self.settings, [make_track(second_source, title="Other")],
        )
        apply_plan(second_plan, self.settings)

        self.assertTrue(
            (self.library / "Artist" / "Album" / "01 - Other.flac").exists(),
        )
        self.assertTrue(first_source.exists())
        self.assertEqual(
            (self.library / "Artist" / "Album" / "01 - Song.flac").read_bytes(),
            b"old-audio",
        )

        undo_last(self.settings)

        self.assertTrue(second_source.exists())
        self.assertFalse(
            (self.library / "Artist" / "Album" / "01 - Other.flac").exists(),
        )
        self.assertTrue(first_source.exists())
        self.assertEqual(
            (self.library / "Artist" / "Album" / "01 - Song.flac").read_bytes(),
            b"old-audio",
        )


if __name__ == "__main__":
    unittest.main()
