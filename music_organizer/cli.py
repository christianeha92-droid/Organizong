from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .config import APP_DIR, Settings
from .covers import CoverService
from .lyrics import LyricsService
from .models import OperationSummary, OrganizePlan, PlanItem
from .operations import apply_plan, undo_last, write_plan_report
from .planner import build_plan
from .scanner import scan_library, scan_overview, source_tracks
from .utils import human_size, path_key

STATUS_LABELS = {
    "move": "se moverán",
    "rename": "se renombrarán para evitar conflicto",
    "same": "ya están en su sitio",
    "duplicate": "duplicados idénticos (se dejan)",
    "conflict": "conflictos (requieren decisión)",
    "missing": "archivos que ya no existen",
    "error": "errores",
    "movido": "movidos",
    "sobrescrito": "sobrescritos",
    "guardado_aparte": "guardados en carpeta nueva",
    "deshecho": "deshechos",
    "omitido": "omitidos",
    "exists": "letras/portadas ya existentes",
    "downloaded": "descargados",
    "embedded": "extraídos de etiquetas",
    "instrumental": "instrumentales",
    "not_found": "sin resultados",
    "would_fetch": "faltan (se descargarían)",
    "would_extract": "se extraerían de etiquetas",
    "would_download": "se descargarían",
}


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def default_source() -> Path:
    parent = APP_DIR.parent
    for name in ("Music", "MusicaSinOrganizar", "Música sin organizar"):
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def default_library(source: Path | None = None) -> Path:
    source = Path(source or default_source())
    parent = APP_DIR.parent
    if source.name.casefold() in {"music", "musica", "música"}:
        return parent / "MusicaOrganizada"
    return source.parent / f"{source.name}Organizada"


def default_root() -> Path:
    return default_source()


def make_settings(args: argparse.Namespace, root_override: Path | None = None) -> Settings:
    source = Path(
        root_override or getattr(args, "source", None) or getattr(args, "root", None) or default_source()
    ).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise SystemExit(f"La carpeta de origen no existe o no es un directorio: {source}")
    library_value = getattr(args, "library", None) or default_library(source)
    library = Path(library_value).expanduser().resolve()
    if library.exists() and not library.is_dir():
        raise SystemExit(f"La carpeta organizada no es un directorio: {library}")
    settings = Settings(
        root=source,
        library_root=library,
        state_root=APP_DIR,
        artist_field=getattr(args, "artist_field", "albumartist"),
        conflict=getattr(args, "conflict", "ask"),
        clean_empty_dirs=getattr(args, "clean_empty_dirs", False),
        overwrite_lyrics=getattr(args, "overwrite_lyrics", False),
        overwrite_covers=getattr(args, "overwrite_covers", False),
        lyrics_provider=getattr(args, "provider", "both"),
        country=getattr(args, "country", "US"),
        workers=getattr(args, "workers", 3),
    )
    return settings


def confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [s/N]: ").strip().casefold()
    except EOFError:
        return False
    return answer in {"s", "si", "sí", "y", "yes"}


def print_header(settings: Settings, title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print(f"Origen:  {settings.root}")
    print(f"Destino: {settings.library_root}")
    print("=" * 72)


def print_scan(tracks, settings: Settings, as_json: bool = False) -> None:
    overview = scan_overview(tracks)
    if as_json:
        print(json.dumps(overview, ensure_ascii=False, indent=2, default=str))
        return
    print(f"Canciones: {overview['tracks']}")
    print(f"Artistas: {overview['artists']}   Álbumes: {overview['albums']}")
    print(f"Con portada incrustada: {overview['with_cover']}   Con letras incrustadas: {overview['with_lyrics']}")
    print(f"Sin artista: {overview['missing_artist']}   Sin álbum: {overview['missing_album']}   Con avisos: {overview['issues']}")
    print(f"Duración aproximada: {overview['sample_duration']}   Tamaño total: {human_size(overview['total_size'])}")
    format_parts = []
    format_sizes = overview["format_sizes"]
    for fmt, count in sorted(overview["formats"].items()):
        format_parts.append(f"{fmt}: {count} ({human_size(format_sizes.get(fmt, 0))})")
    print("Formatos: " + ", ".join(format_parts))
    if overview["issues"]:
        print("\nPrimeras pistas que conviene revisar:")
        shown = 0
        for track in tracks:
            if not track.issues:
                continue
            display_path = track.path
            for root in (settings.root, settings.library_root):
                try:
                    display_path = track.path.relative_to(root)
                    break
                except ValueError:
                    continue
            print(f"  - {display_path}")
            print(f"    {', '.join(track.issues)}")
            shown += 1
            if shown >= 15:
                break


def _info_line(info) -> str:
    if info is None:
        return "desconocido"
    parts = [info.format_label, human_size(info.size)]
    if info.duration:
        minutes, seconds = divmod(int(round(info.duration)), 60)
        parts.append(f"{minutes}:{seconds:02d}")
    if info.bitrate:
        parts.append(f"{round(info.bitrate / 1000)} kbps")
    return " · ".join(parts)


def _format_summary(plan: OrganizePlan) -> str:
    totals: dict[str, list[int]] = {}
    for track in plan.tracks:
        fmt = (track.format or "desconocido").lower()
        item = totals.setdefault(fmt, [0, 0])
        item[0] += 1
        item[1] += max(0, track.size)
    return ", ".join(
        f"{fmt}: {count} ({human_size(size)})"
        for fmt, (count, size) in sorted(totals.items())
    )


def print_plan(plan: OrganizePlan, settings: Settings, limit: int = 25) -> None:
    counts = plan.counts()
    print(f"Pistas analizadas: {len(plan.tracks)}   Álbumes destino: {plan.albums}")
    print(f"Formatos de origen: {_format_summary(plan)}")
    if plan.already_organized:
        print(
            f"\nEl origen ya tiene estructura Artista/Álbum "
            f"({plan.organized_matches}/{len(plan.audio_items)} pistas)."
        )
        print("Se comparará cada canción con la carpeta organizada antes de moverla.")
    else:
        print(
            f"\nEstructura Artista/Álbum detectada en el origen: "
            f"{plan.organized_matches}/{len(plan.audio_items)} pistas."
        )
    for status, count in sorted(counts.items()):
        print(f"  {STATUS_LABELS.get(status, status)}: {count}")
    changes = plan.changes
    if changes:
        print("\nPrimeros movimientos:")
        for item in changes[:limit]:
            try:
                source = item.source.relative_to(settings.root)
            except ValueError:
                source = item.source
            try:
                target = item.target.relative_to(settings.library_root)
            except ValueError:
                target = item.target
            print(f"  {source}  ->  {target}")
        if len(changes) > limit:
            print(f"  ... y {len(changes) - limit} más")
    conflicts = plan.conflicts
    if conflicts:
        print(f"\nConflictos que necesitan decisión: {len(conflicts)}")
        for item in conflicts[:10]:
            title = item.track.title if item.track else item.source.name
            artist = item.track.display_artist if item.track else ""
            print(f"  - {artist} · {title}")
            print(f"    Origen:  {_info_line(item.source_info)}")
            print(f"    Destino: {_info_line(item.target_info)}")
            print(f"    Alternativa: {item.alternate_target}")
        if len(conflicts) > 10:
            print(f"  ... y {len(conflicts) - 10} conflictos más")
    duplicates = [
        item for item in plan.audio_items
        if item.status == "duplicate" and item.target_info is not None
    ]
    if duplicates:
        print(f"\nCanciones que ya existen en el destino (idénticas): {len(duplicates)}")
        for item in duplicates[:10]:
            title = item.track.title if item.track else item.source.name
            artist = item.track.display_artist if item.track else ""
            print(f"  - {artist} · {title}")
            print(f"    Origen:  {_info_line(item.source_info)}")
            print(f"    Destino: {_info_line(item.target_info)}")
        if len(duplicates) > 10:
            print(f"  ... y {len(duplicates) - 10} duplicados más")
    if plan.items:
        report = write_plan_report(settings, plan, "plan")
        print(f"\nInforme CSV: {report}")


def print_summary(summary: OperationSummary, settings: Settings) -> None:
    print()
    print(f"Resultado de {summary.operation}:")
    for status, count in sorted(summary.counts.items()):
        print(f"  {STATUS_LABELS.get(status, status)}: {count}")
    if summary.journal_path:
        print(f"Registro para deshacer: {summary.journal_path}")
    if summary.report_path:
        print(f"Informe: {summary.report_path}")
    if summary.details:
        print("\nDetalles:")
        for detail in summary.details[:30]:
            print(f"  - {detail}")
        if len(summary.details) > 30:
            print(f"  ... y {len(summary.details) - 30} más")


def progress_printer(label: str) -> Callable[[int, int, str], None]:
    last = {"value": -1}

    def update(done: int, total: int, current: str) -> None:
        if total <= 0:
            return
        percent = int(done * 100 / total)
        if percent == last["value"] and done != total:
            return
        last["value"] = percent
        name = current if len(current) <= 70 else "..." + current[-67:]
        print(f"\r{label}: {done}/{total} ({percent:3d}%) {name:<70}", end="", flush=True)
        if done == total:
            print()

    return update


def cmd_scan(args: argparse.Namespace) -> int:
    settings = make_settings(args)
    print_header(settings, f"Análisis de la biblioteca  ·  v{__version__}")
    tracks = scan_library(settings, progress_printer("Analizando"), include_library=True)
    print()
    print_scan(tracks, settings, as_json=args.json)
    return 0


def resolve_conflicts_cli(plan: OrganizePlan, settings: Settings) -> dict[str, str] | None:
    conflicts = plan.conflicts
    if not conflicts:
        return {}
    print(f"\nHay {len(conflicts)} conflictos. Elige qué hacer con cada uno.")
    decisions: dict[str, str] = {}
    all_decision: str | None = None
    choices = {
        "s": "overwrite", "c": "keep", "o": "skip",
        "S": "overwrite", "C": "keep", "O": "skip",
    }
    for index, item in enumerate(conflicts, start=1):
        key = path_key(item.source)
        if all_decision:
            decisions[key] = all_decision
            continue
        title = item.track.title if item.track else item.source.name
        artist = item.track.display_artist if item.track else ""
        print(f"\n[{index}/{len(conflicts)}] {artist} · {title}")
        print(f"  Origen:  {_info_line(item.source_info)}")
        print(f"  Destino: {_info_line(item.target_info)}")
        print(f"  Carpeta nueva si se conserva: {item.alternate_target}")
        while True:
            try:
                answer = input(
                    "  [s]obrescribir / [c]onservar ambos / [o]mitir / "
                    "[S/C/O] para todos / [x] cancelar: "
                ).strip()
            except EOFError:
                print()
                return None
            if answer in {"x", "X"}:
                return None
            decision = choices.get(answer)
            if decision is None:
                print("  Opción no válida.")
                continue
            decisions[key] = decision
            if answer in {"S", "C", "O"}:
                all_decision = decision
            break
    return decisions


def cmd_organize(args: argparse.Namespace, ask: bool = True) -> int:
    settings = make_settings(args)
    print_header(settings, "Organizar por artista / álbum")
    tracks = scan_library(settings, progress_printer("Leyendo etiquetas"))
    print()
    plan = build_plan(settings, tracks)
    print_plan(plan, settings)
    changes = len(plan.changes)
    if not changes:
        print("\nNo hay archivos que mover.")
        return 0
    if not args.apply:
        print("\nModo simulación: no se ha movido nada. Añade --apply para aplicar.")
        return 0
    decisions: dict[str, str] | None = None
    if plan.conflicts and settings.conflict == "ask":
        decisions = resolve_conflicts_cli(plan, settings)
        if decisions is None:
            print("Cancelado.")
            return 0
    if ask and not getattr(args, "no_confirm", False) and not confirm(
        f"¿Mover {changes} archivos (incluyendo letras/portadas asociadas)?"
    ):
        print("Cancelado.")
        return 0
    summary = apply_plan(
        plan, settings, progress_printer("Organizando"), decisions=decisions,
    )
    print_summary(summary, settings)
    return 0 if summary.errors == 0 else 1


def _selected_tracks(settings: Settings, limit: int | None = None):
    tracks = scan_library(
        settings, progress_printer("Leyendo etiquetas"), include_library=True,
    )
    if limit:
        return tracks[:limit]
    return tracks


def cmd_lyrics(args: argparse.Namespace, ask: bool = True) -> int:
    settings = make_settings(args)
    print_header(settings, "Descargar letras")
    tracks = _selected_tracks(settings, args.limit)
    service = LyricsService(settings)
    if not args.apply:
        summary = service.run(tracks, apply=False, limit=args.limit)
        print_summary(summary, settings)
        print("\nModo simulación: no se ha descargado nada. Añade --apply para descargar.")
        return 0
    missing = sum(1 for track in tracks if service.inspect(track) in {"would_fetch", "would_extract"})
    if ask and not getattr(args, "no_confirm", False) and missing and not confirm(
        f"¿Buscar/descargar letras para {missing} canciones?"
    ):
        print("Cancelado.")
        return 0
    summary = service.run(
        tracks, apply=True, overwrite=args.overwrite, limit=args.limit,
        progress=progress_printer("Letras"),
    )
    print_summary(summary, settings)
    return 0 if summary.errors == 0 else 1


def cmd_covers(args: argparse.Namespace, ask: bool = True) -> int:
    settings = make_settings(args)
    print_header(settings, "Descargar portadas")
    tracks = _selected_tracks(settings, args.limit)
    service = CoverService(settings)
    if not args.apply:
        summary = service.run(tracks, apply=False, limit=args.limit)
        print_summary(summary, settings)
        print("\nModo simulación: no se ha descargado nada. Añade --apply para descargar.")
        return 0
    missing = sum(1 for group in _groups_for_cli(tracks) if service.inspect(group) in {"would_fetch", "would_extract"})
    if ask and not getattr(args, "no_confirm", False) and missing and not confirm(
        f"¿Buscar/descargar portadas para {missing} álbumes?"
    ):
        print("Cancelado.")
        return 0
    summary = service.run(
        tracks, apply=True, overwrite=args.overwrite, limit=args.limit,
        progress=progress_printer("Portadas"),
    )
    print_summary(summary, settings)
    return 0 if summary.errors == 0 else 1


def _groups_for_cli(tracks):
    from .covers import _group_tracks
    return _group_tracks(tracks)


def cmd_all(args: argparse.Namespace, ask: bool = True) -> int:
    settings = make_settings(args)
    print_header(settings, "Organizar + letras + portadas")
    tracks = scan_library(
        settings, progress_printer("Leyendo etiquetas"), include_library=True,
    )
    print()
    plan = build_plan(settings, source_tracks(settings, tracks))
    print_plan(plan, settings)
    changes = len(plan.changes)

    if args.apply:
        decisions: dict[str, str] | None = None
        if plan.conflicts and settings.conflict == "ask":
            decisions = resolve_conflicts_cli(plan, settings)
            if decisions is None:
                print("Cancelado.")
                return 0
        action = (
            f"Se moverán {changes} archivos"
            if changes else "No hay archivos que mover"
        )
        if ask and not getattr(args, "no_confirm", False) and not confirm(
            f"{action}; después se buscarán letras y portadas faltantes. ¿Continuar?"
        ):
            print("Cancelado.")
            return 0
        if changes:
            summary = apply_plan(
                plan, settings, progress_printer("Organizando"), decisions=decisions,
            )
            print_summary(summary, settings)
            if summary.errors:
                print("\nSe detiene el proceso porque hubo errores al mover archivos.")
                return 1
        else:
            print("\nNo hay archivos que mover; se continúa con letras y portadas.")
        tracks = scan_library(
            settings, progress_printer("Releyendo biblioteca"), include_library=True,
        )
    else:
        print("\nModo simulación para la organización.")

    cover_service = CoverService(settings)
    if args.apply:
        cover_summary = cover_service.run(
            tracks, apply=True, overwrite=args.overwrite_covers,
            progress=progress_printer("Portadas"),
        )
    else:
        cover_summary = cover_service.run(tracks, apply=False)
    print_summary(cover_summary, settings)

    lyrics_service = LyricsService(settings)
    if args.apply:
        lyrics_summary = lyrics_service.run(
            tracks, apply=True, overwrite=args.overwrite_lyrics,
            progress=progress_printer("Letras"),
        )
    else:
        lyrics_summary = lyrics_service.run(tracks, apply=False)
    print_summary(lyrics_summary, settings)
    if not args.apply:
        print("\nModo simulación: no se ha modificado ni descargado nada. Añade --apply para ejecutar.")
    return 0 if cover_summary.errors == 0 and lyrics_summary.errors == 0 else 1


def cmd_undo(args: argparse.Namespace, ask: bool = True) -> int:
    settings = make_settings(args)
    print_header(settings, "Deshacer la última sesión")
    if ask and not getattr(args, "no_confirm", False) and not confirm(
        "¿Deshacer los cambios de la última sesión?"
    ):
        print("Cancelado.")
        return 0
    summary = undo_last(settings, progress_printer("Deshaciendo"))
    print_summary(summary, settings)
    return 0 if summary.errors == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="organizador.py",
        description="Organiza canciones en carpetas Artista/Álbum y descarga letras y portadas.",
    )
    parser.add_argument(
        "--source", "--root", dest="source",
        help="Carpeta de música sin organizar (por defecto, D:\\Music si existe).",
    )
    parser.add_argument(
        "--library", "--dest", dest="library",
        help="Carpeta de música organizada (por defecto, D:\\MusicaOrganizada).",
    )
    parser.add_argument("--workers", type=int, default=3, help="Descargas simultáneas (1-8).")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Analiza la biblioteca sin cambiar nada.")
    scan.add_argument("--json", action="store_true", help="Salida JSON.")
    scan.set_defaults(func=cmd_scan)

    organize = sub.add_parser("organize", help="Mueve archivos a Artista/Álbum.")
    organize.add_argument("--apply", action="store_true", help="Aplicar los movimientos.")
    organize.add_argument("--artist-field", choices=["albumartist", "artist"], default="albumartist")
    organize.add_argument(
        "--conflict", choices=["ask", "overwrite", "keep", "skip"], default="ask",
        help="Qué hacer si la canción ya existe en el destino.",
    )
    organize.add_argument("--clean-empty-dirs", action="store_true")
    organize.add_argument(
        "--yes", action="store_true", help="No pedir la confirmación final.",
    )
    organize.set_defaults(func=cmd_organize)

    lyrics = sub.add_parser("lyrics", help="Busca y guarda letras .lrc.")
    lyrics.add_argument("--apply", action="store_true", help="Descargar y guardar.")
    lyrics.add_argument("--overwrite", action="store_true", help="Sobrescribir .lrc existentes.")
    lyrics.add_argument("--limit", type=int, default=None, help="Procesar solo las primeras N pistas.")
    lyrics.add_argument("--provider", choices=["lrclib", "lyricsovh", "both"], default="both")
    lyrics.add_argument(
        "--yes", action="store_true", help="No pedir la confirmación final.",
    )
    lyrics.set_defaults(func=cmd_lyrics)

    covers = sub.add_parser("covers", help="Extrae o descarga portadas de álbum.")
    covers.add_argument("--apply", action="store_true", help="Guardar portadas.")
    covers.add_argument("--overwrite", action="store_true", help="Sobrescribir portadas existentes.")
    covers.add_argument("--limit", type=int, default=None, help="Procesar solo los primeros N álbumes.")
    covers.add_argument("--country", default="US", help="País de iTunes, por ejemplo US, MX, JP o ES.")
    covers.add_argument(
        "--yes", action="store_true", help="No pedir la confirmación final.",
    )
    covers.set_defaults(func=cmd_covers)

    all_parser = sub.add_parser("all", help="Organizar, portadas y letras en un solo paso.")
    all_parser.add_argument("--apply", action="store_true")
    all_parser.add_argument("--artist-field", choices=["albumartist", "artist"], default="albumartist")
    all_parser.add_argument(
        "--conflict", choices=["ask", "overwrite", "keep", "skip"], default="ask",
        help="Qué hacer si la canción ya existe en el destino.",
    )
    all_parser.add_argument("--clean-empty-dirs", action="store_true")
    all_parser.add_argument("--overwrite-covers", action="store_true")
    all_parser.add_argument("--overwrite-lyrics", action="store_true")
    all_parser.add_argument("--country", default="US")
    all_parser.add_argument(
        "--yes", action="store_true", help="No pedir la confirmación final.",
    )
    all_parser.set_defaults(func=cmd_all)

    undo = sub.add_parser("undo", help="Deshace la última sesión registrada.")
    undo.add_argument(
        "--yes", action="store_true", help="No pedir la confirmación final.",
    )
    undo.set_defaults(func=cmd_undo)
    return parser


def interactive_menu(settings: Settings) -> int:
    print_header(settings, f"Organizador de Música v{__version__}")
    while True:
        print("\n1. Analizar biblioteca")
        print("2. Vista previa de organización")
        print("3. Organizar archivos (aplicar)")
        print("4. Descargar letras (aplicar)")
        print("5. Descargar portadas (aplicar)")
        print("6. Hacer todo (organizar + letras + portadas)")
        print("7. Deshacer última sesión")
        print("8. Cambiar carpetas de origen y destino")
        print("0. Salir")
        try:
            option = input("\nElige una opción: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        args = argparse.Namespace(
            source=str(settings.root), library=str(settings.library_root),
            workers=settings.workers,
            artist_field=settings.artist_field, conflict=settings.conflict,
            clean_empty_dirs=settings.clean_empty_dirs,
            overwrite_lyrics=settings.overwrite_lyrics,
            overwrite_covers=settings.overwrite_covers,
            provider=settings.lyrics_provider, country=settings.country,
            apply=False, overwrite=False, limit=None, json=False, no_confirm=False,
        )
        try:
            if option == "1":
                tracks = scan_library(
                    settings, progress_printer("Analizando"), include_library=True,
                )
                print()
                print_scan(tracks, settings)
            elif option == "2":
                cmd_organize(args, ask=False)
            elif option == "3":
                args.apply = True
                cmd_organize(args)
            elif option == "4":
                args.apply = True
                cmd_lyrics(args)
            elif option == "5":
                args.apply = True
                cmd_covers(args)
            elif option == "6":
                args.apply = True
                cmd_all(args)
            elif option == "7":
                cmd_undo(args)
            elif option == "8":
                new_source = input("Carpeta de música sin organizar: ").strip().strip('"')
                if not new_source:
                    continue
                candidate = Path(new_source).expanduser().resolve()
                if not candidate.is_dir():
                    print("La carpeta de origen no existe.")
                    continue
                suggested = default_library(candidate)
                new_library = input(
                    f"Carpeta de música organizada [{suggested}]: "
                ).strip().strip('"')
                library = Path(new_library or suggested).expanduser().resolve()
                settings = Settings(
                    root=candidate,
                    library_root=library,
                    state_root=APP_DIR,
                    artist_field=settings.artist_field,
                    conflict=settings.conflict,
                    clean_empty_dirs=settings.clean_empty_dirs,
                    overwrite_lyrics=settings.overwrite_lyrics,
                    overwrite_covers=settings.overwrite_covers,
                    lyrics_provider=settings.lyrics_provider,
                    country=settings.country,
                    workers=settings.workers,
                )
                print(f"Origen:  {settings.root}")
                print(f"Destino: {settings.library_root}")
            elif option == "0":
                return 0
            else:
                print("Opción no válida.")
        except KeyboardInterrupt:
            print("\nOperación cancelada.")
        except SystemExit as exc:
            print(f"Error: {exc}")
        except Exception as exc:
            print(f"Error inesperado: {exc}")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        source = default_source()
        if not source.exists():
            print(f"No existe la carpeta de origen: {source}")
            return 1
        return interactive_menu(Settings(
            root=source, library_root=default_library(source), state_root=APP_DIR,
        ))
    args = parser.parse_args(argv)
    args.no_confirm = bool(getattr(args, "yes", False))
    if not getattr(args, "command", None):
        source = Path(args.source).expanduser().resolve() if args.source else default_source()
        if not source.is_dir():
            print(f"No existe la carpeta de origen: {source}")
            return 1
        library = Path(args.library).expanduser().resolve() if args.library else default_library(source)
        return interactive_menu(Settings(
            root=source, library_root=library, state_root=APP_DIR, workers=args.workers,
        ))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
