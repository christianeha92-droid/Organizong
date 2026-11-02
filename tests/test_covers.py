from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music_organizer.config import Settings
from music_organizer.covers import (
    CoverImage,
    CoverService,
    _image_from_bytes,
    find_existing_cover,
)
from music_organizer.models import Track

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-data"
JPG = b"\xff\xd8\xff" + b"fake-jpg-data"


def make_track(path: Path, *, album: str = "Album", artist: str = "Artist") -> Track:
    return Track(
        path=path,
        artist=artist,
        album_artist=artist,
        album=album,
        title="Song",
        duration=180.0,
        format=path.suffix.lower().lstrip("."),
    )


class CoverImageTests(unittest.TestCase):
    def test_image_extension_detection(self) -> None:
        png = _image_from_bytes(PNG, "")
        jpg = _image_from_bytes(JPG, "")
        self.assertIsNotNone(png)
        self.assertIsNotNone(jpg)
        self.assertEqual(png.extension, ".png")
        self.assertEqual(jpg.extension, ".jpg")

    def test_invalid_image_is_rejected(self) -> None:
        self.assertIsNone(_image_from_bytes(b"not an image"))
        self.assertIsNone(_image_from_bytes(b""))


class CoverServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(root=self.root, state_root=self.root)
        self.service = CoverService(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_track(self, relative: str = "Artist/Album/01 - Song.flac", **kwargs) -> Track:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return make_track(path, **kwargs)

    def test_find_existing_cover_priority_and_ignores_other_images(self) -> None:
        folder = self.root / "Artist" / "Album"
        folder.mkdir(parents=True)
        (folder / "random.jpg").write_bytes(JPG)
        (folder / "folder.png").write_bytes(PNG)
        (folder / "cover.jpg").write_bytes(JPG)

        result = find_existing_cover(folder)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "cover.jpg")

    def test_inspect_existing_embedded_and_missing(self) -> None:
        existing = self.make_track("a/Album/01 - Song.flac")
        (existing.path.parent / "cover.jpg").write_bytes(JPG)
        embedded = self.make_track("b/Album/01 - Song.flac")
        embedded.has_cover = True
        missing = self.make_track("c/Album/01 - Song.flac")

        self.assertEqual(self.service.inspect([existing]), "exists")
        self.assertEqual(self.service.inspect([embedded]), "would_extract")
        self.assertEqual(self.service.inspect([missing]), "would_fetch")

    def test_candidate_acceptance(self) -> None:
        self.assertTrue(self.service._accept_candidate(
            "Daft Punk", "Discovery", "Daft Punk", "Discovery",
        ))
        self.assertFalse(self.service._accept_candidate(
            "Daft Punk", "Discovery", "Otro artista", "Otro álbum",
        ))
        self.assertFalse(self.service._accept_candidate(
            "Daft Punk", "Unknown Album", "Daft Punk", "Discovery",
        ))

    def test_embedded_cover_is_extracted_for_group(self) -> None:
        first = self.make_track("Artist/Album/01 - One.flac")
        second = self.make_track("Artist/Album/02 - Two.flac")
        first.has_cover = True
        second.has_cover = True
        image = CoverImage(data=PNG, mime="image/png", extension=".png")

        with patch("music_organizer.covers.extract_embedded_cover", return_value=image):
            summary = self.service.run([first, second], apply=True)

        cover = first.path.parent / "cover.png"
        self.assertEqual(summary.changed, 1)
        self.assertTrue(cover.exists())
        self.assertEqual(cover.read_bytes(), PNG)
        self.assertIsNotNone(summary.journal_path)

    def test_online_cover_is_downloaded(self) -> None:
        track = self.make_track()
        image = CoverImage(data=JPG, mime="image/jpeg", extension=".jpg")

        with patch.object(self.service, "_fetch_online", return_value=(image, "iTunes")):
            summary = self.service.run([track], apply=True)

        self.assertEqual(summary.changed, 1)
        self.assertEqual((track.path.parent / "cover.jpg").read_bytes(), JPG)

    def test_existing_cover_is_not_overwritten(self) -> None:
        track = self.make_track()
        cover = track.path.parent / "cover.jpg"
        cover.write_bytes(b"local")

        with patch.object(self.service, "_fetch_online") as fetch:
            summary = self.service.run([track], apply=True)

        fetch.assert_not_called()
        self.assertEqual(summary.counts, {"exists": 1})
        self.assertEqual(cover.read_bytes(), b"local")

    def test_overwrite_backs_up_existing_cover(self) -> None:
        track = self.make_track()
        cover = track.path.parent / "cover.jpg"
        cover.write_bytes(b"old")
        image = CoverImage(data=JPG, mime="image/jpeg", extension=".jpg")

        with patch.object(self.service, "_fetch_online", return_value=(image, "iTunes")):
            summary = self.service.run([track], apply=True, overwrite=True)

        self.assertEqual(summary.changed, 1)
        self.assertEqual(cover.read_bytes(), JPG)
        self.assertIsNotNone(summary.journal_path)
        backups = list(self.settings.backup_dir.rglob("cover.jpg"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old")

    def test_not_found_is_reported(self) -> None:
        track = self.make_track()

        with patch.object(self.service, "_fetch_online", return_value=(None, "sin resultados")):
            summary = self.service.run([track], apply=True)

        self.assertEqual(summary.counts, {"not_found": 1})
        self.assertEqual(summary.errors, 0)
        self.assertFalse((track.path.parent / "cover.jpg").exists())

    def test_dry_run_does_not_write(self) -> None:
        track = self.make_track()

        summary = self.service.run([track], apply=False)

        self.assertEqual(summary.counts, {"would_fetch": 1})
        self.assertEqual(summary.changed, 1)
        self.assertFalse((track.path.parent / "cover.jpg").exists())

    def test_grouping_treats_same_folder_and_album_as_one_cover(self) -> None:
        first = self.make_track("Artist/Album/01 - One.flac", album="Album")
        second = self.make_track("Artist/Album/02 - Two.flac", album="Album")
        image = CoverImage(data=JPG, mime="image/jpeg", extension=".jpg")

        with patch.object(self.service, "_fetch_online", return_value=(image, "Deezer")) as fetch:
            summary = self.service.run([first, second], apply=True)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(summary.changed, 1)
        self.assertEqual(summary.total, 1)


if __name__ == "__main__":
    unittest.main()
