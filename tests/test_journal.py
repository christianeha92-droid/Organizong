from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from music_organizer.config import Settings
from music_organizer.journal import (
    latest_run,
    start_run,
    undo_run,
)
from music_organizer.utils import sha256_bytes


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(root=self.root, state_root=self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_start_run_writes_header_and_unique_files(self) -> None:
        first = start_run(self.settings, "organize")
        second = start_run(self.settings, "organize")

        self.assertNotEqual(first.path, second.path)
        self.assertTrue(first.path.exists())
        payload = json.loads(first.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["type"], "header")
        self.assertEqual(payload["operation"], "organize")
        self.assertEqual(payload["root"], str(self.root))

    def test_undo_restores_moved_file(self) -> None:
        source = self.root / "incoming" / "song.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"audio")
        target = self.root / "Artist" / "Album" / "01 - Song.flac"
        target.parent.mkdir(parents=True)
        journal = start_run(self.settings, "organize")
        journal.log_mkdir(target.parent)
        source.rename(target)
        journal.log_move(source, target)

        counts = undo_run(self.settings, journal.path)

        self.assertEqual(counts, {"undone": 2, "skipped": 0, "errors": 0})
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())
        self.assertIsNone(latest_run(self.settings))

    def test_undo_removes_created_file_and_restores_backup(self) -> None:
        target = self.root / "Artist" / "Album" / "cover.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old cover")
        journal = start_run(self.settings, "covers")
        backup = journal.backup_file(target)
        self.assertIsNotNone(backup)
        target.write_bytes(b"new cover")
        journal.log_created(target, sha256_bytes(b"new cover"))

        counts = undo_run(self.settings, journal.path)

        self.assertEqual(counts["undone"], 2)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(target.read_bytes(), b"old cover")

    def test_undo_does_not_delete_modified_created_file(self) -> None:
        target = self.root / "letra.lrc"
        target.write_text("texto nuevo", encoding="utf-8")
        journal = start_run(self.settings, "lyrics")
        journal.log_created(target, sha256_bytes(b"texto original"))

        counts = undo_run(self.settings, journal.path)

        self.assertEqual(counts["skipped"], 1)
        self.assertTrue(target.exists())

    def test_undo_is_idempotent(self) -> None:
        target = self.root / "letra.lrc"
        target.write_text("texto", encoding="utf-8")
        journal = start_run(self.settings, "lyrics")
        journal.log_created(target, sha256_bytes(b"texto"))

        first = undo_run(self.settings, journal.path)
        second = undo_run(self.settings, journal.path)

        self.assertEqual(first["undone"], 1)
        self.assertEqual(second["already"], 1)
        self.assertEqual(second["undone"], 0)

    def test_latest_run_can_include_undone(self) -> None:
        journal = start_run(self.settings, "lyrics")
        journal._append({"type": "undone"})

        self.assertIsNone(latest_run(self.settings))
        self.assertEqual(latest_run(self.settings, include_undone=True), journal.path)

    def test_backup_file_keeps_relative_path(self) -> None:
        target = self.root / "Artist" / "Album" / "cover.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"image")
        journal = start_run(self.settings, "covers")

        backup = journal.backup_file(target)

        self.assertIsNotNone(backup)
        self.assertEqual(
            backup.relative_to(self.settings.backup_dir),
            Path(journal.run_id) / "Artist" / "Album" / "cover.jpg",
        )
        self.assertEqual(backup.read_bytes(), b"image")


if __name__ == "__main__":
    unittest.main()
