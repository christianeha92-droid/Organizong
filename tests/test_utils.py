from __future__ import annotations

import unittest
from pathlib import Path

from music_organizer.utils import (
    album_match_score,
    artist_similarity,
    fit_path,
    format_track_filename,
    infer_title_from_filename,
    normalize_match,
    path_key,
    sanitize_component,
    text_similarity,
)


class UtilsTests(unittest.TestCase):
    def test_sanitize_component(self) -> None:
        self.assertEqual(sanitize_component('AC/DC: Live? *'), "AC-DC - Live")
        self.assertEqual(sanitize_component("  ..  "), "Sin nombre")
        self.assertEqual(sanitize_component("CON"), "_CON")
        self.assertEqual(sanitize_component("A" * 200, max_len=30), "A" * 30)

    def test_infer_title(self) -> None:
        self.assertEqual(infer_title_from_filename(Path("01 - The Scientist.flac")), "The Scientist")
        self.assertEqual(infer_title_from_filename(Path("04. Cita en el Quirófano.flac")), "Cita en el Quirófano")
        self.assertEqual(infer_title_from_filename(Path("1-03 Song.mp3")), "Song")
        self.assertEqual(infer_title_from_filename(Path("01 - Can't Help Falling in Love.flac")), "Can't Help Falling in Love")

    def test_format_track_filename(self) -> None:
        self.assertEqual(
            format_track_filename("A/B: C?", ".flac", 3, 1, False),
            "03 - A-B - C.flac",
        )
        self.assertEqual(
            format_track_filename("Song", ".mp3", 2, 2, True),
            "2-02 - Song.mp3",
        )

    def test_fit_path(self) -> None:
        target = fit_path(Path(r"D:\Music"), "A" * 80, "B" * 90, "C" * 120 + ".flac", max_path=240)
        self.assertLessEqual(len(str(target)), 240)
        self.assertTrue(str(target).endswith(".flac"))

    def test_matching(self) -> None:
        self.assertEqual(normalize_match("Beyoncé - Halo (Live)"), "beyonce halo")
        self.assertGreater(text_similarity("The Scientist", "The Scientist (Live)"), 0.7)
        self.assertGreater(artist_similarity("Daft Punk, Pharrell Williams", "Daft Punk"), 0.9)
        self.assertGreater(
            album_match_score("Coldplay", "A Rush of Blood to the Head", "Coldplay", "A Rush of Blood to the Head"),
            0.95,
        )
        self.assertGreater(
            album_match_score("Various Artists", "60's Hits", "Various Artists", "60's Hits"),
            0.95,
        )

    def test_path_key_is_case_insensitive(self) -> None:
        self.assertEqual(path_key(Path("D:/Music/A/File.flac")), path_key(Path("d:/music/a/file.flac")))


if __name__ == "__main__":
    unittest.main()
