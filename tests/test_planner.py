from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_organizer.config import Settings
from music_organizer.models import Track
from music_organizer.planner import artist_for_folder, build_plan


def make_track(
    path: Path,
    *,
    artist: str = "Artist",
    album_artist: str = "",
    album: str = "Album",
    title: str = "Song",
    number: int | None = 1,
    disc: int | None = None,
) -> Track:
    return Track(
        path=path,
        artist=artist,
        album_artist=album_artist or artist,
        album=album,
        title=title,
        track_number=number,
        disc_number=disc,
        duration=180.0,
        format=path.suffix.lower().lstrip("."),
    )


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(root=self.root, state_root=self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, data: bytes = b"audio") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_moves_audio_and_companions_to_artist_album(self) -> None:
        source = self.write("incoming/one.flac")
        self.write("incoming/one.lrc", b"[00:01.00]Hola")
        self.write("incoming/one.jpg", b"image")
        self.write("incoming/cover.jpg", b"album cover")
        track = make_track(source, artist="Daft Punk", album="Discovery", title="One More Time")

        plan = build_plan(self.settings, [track])

        target = self.root / "Daft Punk" / "Discovery" / "01 - One More Time.flac"
        self.assertEqual(plan.target_for_track[source], target)
        audio = plan.audio_items[0]
        self.assertEqual((audio.status, audio.target), ("move", target))
        companions = {item.source.name: item for item in plan.companion_items}
        self.assertEqual(set(companions), {"one.lrc", "one.jpg"})
        self.assertEqual(companions["one.lrc"].target, target.with_suffix(".lrc"))
        self.assertEqual(companions["one.jpg"].target, target.with_name("01 - One More Time.jpg"))
        self.assertTrue(all(item.status == "move" for item in companions.values()))

    def test_uses_artist_field_when_configured(self) -> None:
        source = self.write("incoming/song.mp3")
        track = make_track(
            source, artist="Artista invitado", album_artist="Artista principal", album="Disco",
        )
        settings = Settings(root=self.root, state_root=self.root, artist_field="artist")

        self.assertEqual(artist_for_folder(track, settings), "Artista invitado")
        plan = build_plan(settings, [track])
        self.assertEqual(plan.audio_items[0].target.parent, self.root / "Artista invitado" / "Disco")

    def test_multi_disc_uses_disc_prefix(self) -> None:
        first = self.write("incoming/d1.flac")
        second = self.write("incoming/d2.flac")
        tracks = [
            make_track(first, album="Grandes éxitos", title="Uno", number=1, disc=1),
            make_track(second, album="Grandes éxitos", title="Dos", number=1, disc=2),
        ]

        plan = build_plan(self.settings, tracks)

        targets = [item.target.name for item in plan.audio_items]
        self.assertEqual(targets, ["1-01 - Uno.flac", "2-01 - Dos.flac"])

    def test_identical_existing_file_is_marked_duplicate(self) -> None:
        source = self.write("incoming/song.flac", b"same-content")
        self.write("Artist/Album/01 - Song.flac", b"same-content")
        track = make_track(source, title="Song")

        plan = build_plan(self.settings, [track])

        self.assertEqual(plan.audio_items[0].status, "duplicate")
        self.assertFalse(plan.changes)

    def test_conflict_reports_both_versions_and_alternate_album(self) -> None:
        source = self.write("incoming/song.flac", b"new-content")
        existing = self.write("Artist/Album/01 - Song.flac", b"old-content!")
        track = make_track(source, title="Song")

        skipped = build_plan(
            Settings(root=self.root, state_root=self.root, conflict="skip"), [track],
        )
        item = skipped.audio_items[0]
        self.assertEqual(item.status, "conflict")
        self.assertEqual(item.target, existing)
        self.assertEqual(item.source_info.format, "flac")
        self.assertEqual(item.source_info.size, len(b"new-content"))
        self.assertEqual(item.target_info.format, "flac")
        self.assertEqual(item.target_info.size, len(b"old-content!"))
        self.assertEqual(
            item.alternate_target,
            self.root / "Artist" / "Album (2)" / "01 - Song.flac",
        )

        kept = build_plan(
            Settings(root=self.root, state_root=self.root, conflict="keep"), [track],
        )
        self.assertEqual(kept.audio_items[0].status, "conflict")
        self.assertEqual(
            kept.audio_items[0].alternate_target.name, "01 - Song.flac",
        )
        self.assertEqual(kept.audio_items[0].alternate_target.parent.name, "Album (2)")

    def test_two_tracks_with_same_target_are_disambiguated(self) -> None:
        first = self.write("incoming/a.flac", b"a")
        second = self.write("incoming/b.flac", b"b")
        tracks = [
            make_track(first, title="Song", number=1),
            make_track(second, title="Song", number=1),
        ]

        plan = build_plan(self.settings, tracks)

        targets = [item.target.name for item in plan.audio_items]
        self.assertEqual(targets, ["01 - Song.flac", "01 - Song (2).flac"])

    def test_companion_conflict_is_reported(self) -> None:
        source = self.write("incoming/song.flac")
        self.write("incoming/song.lrc", b"lyrics")
        self.write("Artist/Album/01 - Song.lrc", b"different lyrics")
        track = make_track(source, title="Song")

        plan = build_plan(self.settings, [track])

        companion = plan.companion_items[0]
        self.assertEqual(companion.status, "conflict")
        self.assertIsNotNone(companion.target_info)
        self.assertIn(companion, plan.changes)

    def test_missing_audio_is_reported(self) -> None:
        missing = self.root / "incoming" / "gone.flac"
        plan = build_plan(self.settings, [make_track(missing)])

        item = plan.audio_items[0]
        self.assertEqual(item.status, "missing")
        self.assertFalse(item.will_change)
        self.assertNotIn(missing, plan.target_for_track)

    def test_same_location_is_marked_same(self) -> None:
        source = self.write("Artist/Album/01 - Song.flac")
        plan = build_plan(self.settings, [make_track(source, title="Song")])

        self.assertEqual(plan.audio_items[0].status, "same")
        self.assertFalse(plan.changes)

    def test_unknown_album_gets_unknown_folder(self) -> None:
        source = self.write("incoming/song.flac")
        track = make_track(source, title="Song")
        track.album = ""

        plan = build_plan(self.settings, [track])

        self.assertEqual(plan.audio_items[0].target.parent.name, "Unknown Album")

    def test_separate_library_receives_the_audio(self) -> None:
        source_root = self.root / "source"
        library_root = self.root / "library"
        source = source_root / "incoming" / "song.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"audio")
        settings = Settings(
            root=source_root, library_root=library_root, state_root=self.root / "state",
        )

        plan = build_plan(settings, [make_track(source, title="Song")])

        self.assertEqual(
            plan.audio_items[0].target,
            library_root / "Artist" / "Album" / "01 - Song.flac",
        )
        self.assertEqual(plan.audio_items[0].status, "move")

    def test_detects_source_with_artist_album_structure(self) -> None:
        source_root = self.root / "source"
        library_root = self.root / "library"
        source = source_root / "Artist" / "Album" / "01 - Song.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"audio")
        settings = Settings(
            root=source_root, library_root=library_root, state_root=self.root / "state",
        )

        plan = build_plan(settings, [make_track(source, title="Song")])

        self.assertEqual(plan.organized_matches, 1)
        self.assertTrue(plan.already_organized)
        self.assertEqual(plan.audio_items[0].status, "move")

    def test_conflicts_in_same_album_share_the_new_album_folder(self) -> None:
        source_root = self.root / "source"
        library_root = self.root / "library"
        first = source_root / "incoming" / "one.flac"
        second = source_root / "incoming" / "two.flac"
        for path, data in ((first, b"new-one"), (second, b"new-two")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name, data in (("01 - One.flac", b"old-one"), ("02 - Two.flac", b"old-two")):
            target = library_root / "Artist" / "Album" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        settings = Settings(
            root=source_root, library_root=library_root,
            state_root=self.root / "state", conflict="keep",
        )
        tracks = [
            make_track(first, title="One", number=1),
            make_track(second, title="Two", number=2),
        ]

        plan = build_plan(settings, tracks)

        alternatives = [item.alternate_target for item in plan.audio_items]
        self.assertEqual(
            {path.parent for path in alternatives},
            {library_root / "Artist" / "Album (2)"},
        )
        self.assertEqual(
            {path.name for path in alternatives}, {"01 - One.flac", "02 - Two.flac"},
        )

    def test_existing_alternate_album_is_not_reused(self) -> None:
        source_root = self.root / "source"
        library_root = self.root / "library"
        source = source_root / "incoming" / "song.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"new")
        target = library_root / "Artist" / "Album" / "01 - Song.flac"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        (library_root / "Artist" / "Album (2)").mkdir(parents=True)
        settings = Settings(
            root=source_root, library_root=library_root,
            state_root=self.root / "state", conflict="keep",
        )

        plan = build_plan(settings, [make_track(source, title="Song")])

        self.assertEqual(
            plan.audio_items[0].alternate_target.parent.name, "Album (3)",
        )

    def test_duplicate_in_separate_library_reports_target_info(self) -> None:
        source_root = self.root / "source"
        library_root = self.root / "library"
        source = source_root / "incoming" / "song.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"same")
        target = library_root / "Artist" / "Album" / "01 - Song.flac"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"same")
        settings = Settings(
            root=source_root, library_root=library_root, state_root=self.root / "state",
        )

        plan = build_plan(settings, [make_track(source, title="Song")])
        item = plan.audio_items[0]

        self.assertEqual(item.status, "duplicate")
        self.assertEqual(item.target_info.size, 4)
        self.assertEqual(item.target_info.path, target)


if __name__ == "__main__":
    unittest.main()
