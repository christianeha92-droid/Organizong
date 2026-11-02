from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music_organizer.config import Settings
from music_organizer.lyrics import LyricsService, _FetchedLyrics, format_lrc
from music_organizer.models import Track


def make_track(path: Path, *, title: str = "Song", artist: str = "Artist", album: str = "Album") -> Track:
    return Track(
        path=path,
        artist=artist,
        album_artist=artist,
        album=album,
        title=title,
        duration=201.0,
        format=path.suffix.lower().lstrip("."),
    )


class LyricsFormatTests(unittest.TestCase):
    def test_format_lrc_adds_headers_and_detects_sync(self) -> None:
        track = make_track(
            Path("Halo.flac"), title="Halo (Live)", artist="Beyoncé", album="B'Day",
        )
        text = "\ufeff[00:01.00]Hola\r\n[00:02.00]Mundo\r\n"

        result = format_lrc(track, text, "LRCLIB", synced=False)

        self.assertIn("[ti:Halo (Live)]", result)
        self.assertIn("[ar:Beyoncé]", result)
        self.assertIn("[al:B'Day]", result)
        self.assertIn("[length:3:21]", result)
        self.assertIn("[by:LRCLIB]", result)
        self.assertIn("[00:01.00]Hola\n[00:02.00]Mundo", result)
        self.assertTrue(result.endswith("\n"))
        self.assertNotIn("\r", result)
        self.assertNotIn("\ufeff", result)

    def test_format_lrc_keeps_existing_headers(self) -> None:
        track = make_track(Path("Song.mp3"), title="Song", artist="Artist", album="Album")
        text = "[ti:Otro título]\n[ar:Otro artista]\n[00:01.00]Letra"

        result = format_lrc(track, text, "Lyrics.ovh", synced=True)

        self.assertNotIn("[ti:Song]", result)
        self.assertNotIn("[ar:Artist]", result)
        self.assertIn("[ti:Otro título]", result)
        self.assertIn("[by:Lyrics.ovh]", result)

    def test_format_lrc_returns_empty_for_blank_text(self) -> None:
        track = make_track(Path("Song.flac"))
        self.assertEqual(format_lrc(track, "   \n", "LRCLIB", False), "")

    def test_format_lrc_plain_text_has_separator(self) -> None:
        track = make_track(Path("Song.flac"))
        result = format_lrc(track, "Solo texto", "Lyrics.ovh", False)
        self.assertIn("[by:Lyrics.ovh]\n\nSolo texto\n", result)


class LyricsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(root=self.root, state_root=self.root, workers=2)
        self.service = LyricsService(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_file(self, name: str = "incoming/Song.flac") -> Track:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return make_track(path)

    def test_inspect_existing_embedded_and_missing(self) -> None:
        existing = self.make_file("a/Song.flac")
        existing.path.with_suffix(".lrc").write_text("letra", encoding="utf-8")
        embedded = self.make_file("b/Song.flac")
        embedded.has_lyrics = True
        missing = self.make_file("c/Song.flac")

        self.assertEqual(self.service.inspect(existing), "exists")
        self.assertEqual(self.service.inspect(embedded), "would_extract")
        self.assertEqual(self.service.inspect(missing), "would_fetch")

    def test_existing_lyrics_are_not_overwritten(self) -> None:
        track = self.make_file()
        track.path.with_suffix(".lrc").write_text("letra local", encoding="utf-8")

        outcome = self.service.fetch_one(track, apply=True)

        self.assertEqual(outcome.status, "exists")
        self.assertEqual(track.path.with_suffix(".lrc").read_text(encoding="utf-8"), "letra local")

    def test_embedded_lyrics_are_extracted(self) -> None:
        track = self.make_file()
        track.has_lyrics = True

        with patch(
            "music_organizer.lyrics.extract_embedded_lyrics",
            return_value=("[00:01.00]Hola", True),
        ):
            outcome = self.service.fetch_one(track, apply=True)

        target = track.path.with_suffix(".lrc")
        self.assertEqual(outcome.status, "downloaded")
        self.assertEqual(outcome.provider, "etiqueta incrustada")
        self.assertTrue(outcome.synced)
        self.assertIn("[by:etiqueta incrustada]", target.read_text(encoding="utf-8"))

    def test_online_lyrics_are_written(self) -> None:
        track = self.make_file()

        with patch.object(
            self.service, "_fetch_online",
            return_value=_FetchedLyrics("LRCLIB", "[00:02.00]Hello", True),
        ):
            outcome = self.service.fetch_one(track, apply=True)

        target = track.path.with_suffix(".lrc")
        self.assertEqual(outcome.status, "downloaded")
        self.assertEqual(outcome.provider, "LRCLIB")
        self.assertTrue(outcome.synced)
        content = target.read_text(encoding="utf-8")
        self.assertIn("[by:LRCLIB]", content)
        self.assertIn("[00:02.00]Hello", content)

    def test_not_found_does_not_create_file(self) -> None:
        track = self.make_file()

        with patch.object(self.service, "_fetch_online", return_value=None):
            outcome = self.service.fetch_one(track, apply=True)

        self.assertEqual(outcome.status, "not_found")
        self.assertFalse(track.path.with_suffix(".lrc").exists())

    def test_network_error_is_reported(self) -> None:
        track = self.make_file()

        with patch.object(self.service, "_fetch_online", side_effect=RuntimeError("sin red")):
            outcome = self.service.fetch_one(track, apply=True)

        self.assertEqual(outcome.status, "error")
        self.assertIn("sin red", outcome.message)

    def test_instrumental_is_not_written(self) -> None:
        track = self.make_file()

        with patch.object(
            self.service, "_fetch_online",
            return_value=_FetchedLyrics("LRCLIB", "", False, instrumental=True),
        ):
            outcome = self.service.fetch_one(track, apply=True)

        self.assertEqual(outcome.status, "instrumental")
        self.assertFalse(track.path.with_suffix(".lrc").exists())

    def test_dry_run_reports_without_writing(self) -> None:
        track = self.make_file()
        track.has_lyrics = True

        summary = self.service.run([track], apply=False)

        self.assertEqual(summary.changed, 1)
        self.assertEqual(summary.counts, {"would_extract": 1})
        self.assertFalse(track.path.with_suffix(".lrc").exists())

    def test_apply_run_writes_lyrics_and_journal(self) -> None:
        first = self.make_file("a/One.flac")
        second = self.make_file("b/Two.flac")

        def fake_fetch(track: Track) -> _FetchedLyrics:
            return _FetchedLyrics("LRCLIB", f"Letra de {track.title}", False)

        with patch.object(self.service, "_fetch_online", side_effect=fake_fetch):
            summary = self.service.run([first, second], apply=True)

        self.assertEqual(summary.changed, 2)
        self.assertEqual(summary.errors, 0)
        self.assertTrue(first.path.with_suffix(".lrc").exists())
        self.assertTrue(second.path.with_suffix(".lrc").exists())
        self.assertIsNotNone(summary.journal_path)
        self.assertTrue(summary.journal_path.exists())


if __name__ == "__main__":
    unittest.main()
