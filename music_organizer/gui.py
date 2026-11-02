from __future__ import annotations

import os
import queue
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, Toplevel, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .config import APP_DIR, Settings
from .covers import CoverService, _group_tracks
from .lyrics import LyricsService
from .models import MediaInfo, OperationSummary, OrganizePlan, PlanItem, Track
from .operations import apply_plan, undo_last, write_plan_report
from .planner import build_plan
from .scanner import scan_library, scan_overview, source_tracks
from .utils import format_duration, human_size, path_key

STATUS_LABELS = {
    "move": "Mover",
    "rename": "Renombrar",
    "same": "Ya está",
    "duplicate": "Duplicado",
    "conflict": "Conflicto",
    "missing": "No existe",
    "error": "Error",
    "exists": "Ya existe",
    "downloaded": "Descargado",
    "embedded": "Extraído",
    "instrumental": "Instrumental",
    "not_found": "Sin resultado",
    "would_fetch": "Faltan",
    "would_extract": "Extraíble",
    "would_download": "Descargable",
    "movido": "Movido",
    "sobrescrito": "Sobrescrito",
    "guardado_aparte": "Guardado aparte",
    "deshecho": "Deshecho",
    "omitido": "Omitido",
}


def _source_default() -> Path:
    parent = APP_DIR.parent
    for name in ("Music", "MusicaSinOrganizar", "Música sin organizar"):
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def _library_default(source: Path | None = None) -> Path:
    source = Path(source or _source_default())
    parent = APP_DIR.parent
    if source.name.casefold() in {"music", "musica", "música"}:
        return parent / "MusicaOrganizada"
    return source.parent / f"{source.name}Organizada"


def _media_text(info: MediaInfo | None) -> str:
    if info is None:
        return "desconocido"
    parts = [info.format_label, human_size(info.size)]
    if info.duration:
        parts.append(format_duration(info.duration))
    if info.bitrate:
        parts.append(f"{round(info.bitrate / 1000)} kbps")
    return " · ".join(parts)


def _format_summary(tracks: list[Track]) -> str:
    totals: dict[str, list[int]] = {}
    for track in tracks:
        fmt = (track.format or "desconocido").lower()
        item = totals.setdefault(fmt, [0, 0])
        item[0] += 1
        item[1] += max(0, track.size)
    return ", ".join(
        f"{fmt}: {count} ({human_size(size)})"
        for fmt, (count, size) in sorted(totals.items())
    )


class ConflictDialog:
    """Pregunta qué hacer con una canción que ya existe en el destino."""

    def __init__(
        self,
        parent: Tk,
        item: PlanItem,
        index: int,
        total: int,
    ) -> None:
        self.parent = parent
        self.decision: str | None = None
        self.apply_all = False
        self.apply_all_var = BooleanVar(value=False)

        self.top = Toplevel(parent)
        self.top.title(f"Conflicto {index} de {total}")
        self.top.transient(parent)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

        frame = ttk.Frame(self.top, padding=14)
        frame.pack(fill="both", expand=True)

        track = item.track
        artist = track.display_artist if track else ""
        album = track.album if track else ""
        title = track.title if track else item.source.name
        ttk.Label(
            frame,
            text=f"{artist} · {album}\n{title}",
            font=("Segoe UI", 11, "bold"),
            wraplength=620,
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            frame,
            text=(
                "La canción ya existe en la carpeta organizada y los archivos son distintos. "
                "Compara el formato y el tamaño antes de decidir."
            ),
            wraplength=620,
        ).pack(anchor="w", pady=(0, 10))

        info_frame = ttk.Frame(frame)
        info_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(info_frame, text="Origen:", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 8),
        )
        ttk.Label(info_frame, text=_media_text(item.source_info)).grid(
            row=0, column=1, sticky="w",
        )
        ttk.Label(info_frame, text="Destino:", font=("Segoe UI", 9, "bold")).grid(
            row=1, column=0, sticky="w", padx=(0, 8),
        )
        ttk.Label(info_frame, text=_media_text(item.target_info)).grid(
            row=1, column=1, sticky="w",
        )
        ttk.Label(info_frame, text="Carpeta nueva:", font=("Segoe UI", 9, "bold")).grid(
            row=2, column=0, sticky="nw", padx=(0, 8), pady=(4, 0),
        )
        ttk.Label(
            info_frame,
            text=str(item.alternate_target or ""),
            wraplength=540,
        ).grid(row=2, column=1, sticky="w", pady=(4, 0))

        ttk.Checkbutton(
            frame,
            text="Aplicar esta decisión a todos los conflictos restantes",
            variable=self.apply_all_var,
        ).pack(anchor="w", pady=(6, 10))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(
            buttons, text="Sobrescribir destino",
            command=lambda: self._choose("overwrite"),
        ).pack(side="left")
        ttk.Button(
            buttons, text="Guardar en carpeta nueva",
            command=lambda: self._choose("keep"),
        ).pack(side="left", padx=6)
        ttk.Button(
            buttons, text="Omitir esta canción",
            command=lambda: self._choose("skip"),
        ).pack(side="left")
        ttk.Button(buttons, text="Cancelar", command=self._cancel).pack(side="right")

    def _choose(self, decision: str) -> None:
        self.decision = decision
        self.apply_all = bool(self.apply_all_var.get())
        self.top.destroy()

    def _cancel(self) -> None:
        self.decision = None
        self.top.destroy()

    def show(self) -> tuple[str | None, bool]:
        self.top.grab_set()
        self.parent.wait_window(self.top)
        return self.decision, self.apply_all


class OrganizerApp:
    def __init__(self, window: Tk) -> None:
        self.window = window
        self.window.title(f"Organizador de Música v{__version__}")
        self.window.geometry("1440x860")
        self.window.minsize(1100, 680)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.stop_event = threading.Event()
        self._on_done: Callable[[object], None] | None = None
        self.tracks: list[Track] = []
        self.plan: OrganizePlan | None = None
        self.buttons: list[ttk.Button] = []

        self.root_var = StringVar(value=str(_source_default()))
        self.library_var = StringVar(value=str(_library_default()))
        self.artist_field_var = StringVar(value="Álbum artista (recomendado)")
        self.clean_var = BooleanVar(value=False)
        self.overwrite_lyrics_var = BooleanVar(value=False)
        self.overwrite_covers_var = BooleanVar(value=False)
        self.simulate_var = BooleanVar(value=True)
        self.status_var = StringVar(value="Listo")
        self.organized_var = StringVar(value="Origen: pendiente de analizar")

        self._build_style()
        self._build_ui()
        self.window.after(250, self.analyze)

    def _build_style(self) -> None:
        style = ttk.Style(self.window)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.window, padding=10)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Organizador de Música", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="  ·  Artista / Álbum  ·  letras .lrc  ·  portadas",
            style="Hint.TLabel",
        ).pack(side="left", padx=(4, 0))

        folder_frame = ttk.LabelFrame(outer, text="Biblioteca", padding=8)
        folder_frame.pack(fill="x", pady=(8, 0))
        folder_frame.columnconfigure(1, weight=1)
        ttk.Label(folder_frame, text="Origen:").grid(row=0, column=0, sticky="w")
        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.root_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(folder_frame, text="Elegir…", command=self.choose_folder).grid(
            row=0, column=2, padx=(0, 4), pady=2,
        )
        ttk.Button(folder_frame, text="Abrir", command=self.open_source).grid(
            row=0, column=3, pady=2,
        )

        ttk.Label(folder_frame, text="Organizada:").grid(row=1, column=0, sticky="w")
        self.library_entry = ttk.Entry(folder_frame, textvariable=self.library_var)
        self.library_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(folder_frame, text="Elegir…", command=self.choose_library).grid(
            row=1, column=2, padx=(0, 4), pady=2,
        )
        ttk.Button(folder_frame, text="Abrir", command=self.open_library).grid(
            row=1, column=3, pady=2,
        )
        self.analyze_button = ttk.Button(folder_frame, text="Analizar", command=self.analyze)
        self.analyze_button.grid(row=0, column=4, rowspan=2, sticky="ns", padx=(10, 0))

        options = ttk.LabelFrame(outer, text="Opciones", padding=8)
        options.pack(fill="x", pady=(8, 0))
        ttk.Label(options, text="Carpeta de artista:").pack(side="left")
        self.artist_combo = ttk.Combobox(
            options,
            textvariable=self.artist_field_var,
            values=["Álbum artista (recomendado)", "Artista de la pista"],
            state="readonly",
            width=30,
        )
        self.artist_combo.pack(side="left", padx=(6, 14))
        ttk.Checkbutton(options, text="Limpiar carpetas vacías", variable=self.clean_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(options, text="Sobrescribir letras", variable=self.overwrite_lyrics_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(options, text="Sobrescribir portadas", variable=self.overwrite_covers_var).pack(side="left", padx=(0, 12))
        self.simulate_check = ttk.Checkbutton(
            options,
            text="Solo simular (no modifica archivos)",
            variable=self.simulate_var,
        )
        self.simulate_check.pack(side="left")

        detection = ttk.Frame(outer)
        detection.pack(fill="x", pady=(4, 0))
        ttk.Label(detection, textvariable=self.organized_var, style="Hint.TLabel").pack(
            side="left", anchor="w",
        )

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self._action_button(actions, "1. Organizar", self.organize)
        self._action_button(actions, "2. Descargar letras", self.download_lyrics)
        self._action_button(actions, "3. Descargar portadas", self.download_covers)
        self._action_button(actions, "Hacer todo", self.do_all)
        self._action_button(actions, "Deshacer última sesión", self.undo)
        ttk.Button(actions, text="Detener", command=self.stop_current).pack(side="right", padx=(6, 0))

        paned = ttk.Panedwindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(10, 0))

        tree_frame = ttk.LabelFrame(paned, text="Vista previa de la organización", padding=4)
        columns = (
            "estado", "artista", "album", "titulo",
            "formato_origen", "tamano_origen",
            "formato_destino", "tamano_destino",
            "origen", "destino",
        )
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "estado": ("Estado", 90),
            "artista": ("Artista", 150),
            "album": ("Álbum", 170),
            "titulo": ("Título", 180),
            "formato_origen": ("Formato origen", 90),
            "tamano_origen": ("Tamaño origen", 90),
            "formato_destino": ("Formato destino", 95),
            "tamano_destino": ("Tamaño destino", 95),
            "origen": ("Origen", 260),
            "destino": ("Destino", 310),
        }
        for key, (title, width) in headings.items():
            self.tree.heading(key, text=title)
            self.tree.column(
                key, width=width, minwidth=55,
                stretch=key in {"album", "titulo", "origen", "destino"},
            )
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("ok", foreground="#777777")
        self.tree.tag_configure("warn", foreground="#a15c00")
        self.tree.tag_configure("error", foreground="#a00000")
        paned.add(tree_frame, weight=4)

        log_frame = ttk.LabelFrame(paned, text="Registro", padding=4)
        self.log_text = ScrolledText(log_frame, height=9, wrap="word", font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill="both", expand=True)
        paned.add(log_frame, weight=1)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, textvariable=self.status_var, width=58, anchor="e").pack(side="left", padx=(10, 0))

        self._log("Listo. La vista previa no modifica archivos.")
        self._log("Desmarca «Solo simular» para aplicar cambios; siempre se pedirá confirmación.")
        self._log(f"Origen:  {self.root_var.get()}")
        self._log(f"Destino: {self.library_var.get()}")

    def _action_button(self, parent: ttk.Frame, text: str, command: Callable[[], None]) -> None:
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=(0, 6))
        self.buttons.append(button)

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _settings(self) -> Settings | None:
        source = Path(self.root_var.get()).expanduser()
        if not source.exists() or not source.is_dir():
            messagebox.showerror("Carpeta no válida", f"No existe la carpeta de origen:\n{source}")
            return None
        library_text = self.library_var.get().strip()
        if not library_text:
            messagebox.showerror("Carpeta no válida", "Indica la carpeta de música organizada.")
            return None
        library = Path(library_text).expanduser()
        if library.exists() and not library.is_dir():
            messagebox.showerror("Carpeta no válida", f"La ruta de destino no es una carpeta:\n{library}")
            return None
        if not library.parent.exists():
            messagebox.showerror("Carpeta no válida", f"No existe la carpeta padre de:\n{library}")
            return None
        artist_field = "albumartist" if self.artist_field_var.get().startswith("Álbum") else "artist"
        return Settings(
            root=source,
            library_root=library,
            state_root=APP_DIR,
            artist_field=artist_field,
            conflict="ask",
            clean_empty_dirs=self.clean_var.get(),
            overwrite_lyrics=self.overwrite_lyrics_var.get(),
            overwrite_covers=self.overwrite_covers_var.get(),
        )

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in self.buttons:
            button.configure(state=state)
        self.analyze_button.configure(state=state)
        self.artist_combo.configure(state="disabled" if busy else "readonly")
        self.simulate_check.configure(state=state)
        self.folder_entry.configure(state=state)
        self.library_entry.configure(state=state)

    def _start_worker(self, label: str, worker: Callable, on_done: Callable[[object], None]) -> None:
        if self.busy:
            return
        self._on_done = on_done
        self.stop_event = threading.Event()
        self._set_busy(True)
        self.status_var.set(label)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.queue = queue.Queue()

        def progress(done: int, total: int, text: str) -> None:
            self.queue.put(("progress", done, total, text))

        def run() -> None:
            try:
                result = worker(progress, self.stop_event)
            except Exception:
                self.queue.put(("error", traceback.format_exc()))
            else:
                self.queue.put(("done", result))

        threading.Thread(target=run, daemon=True).start()
        self.window.after(80, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, done, total, text = message
                    if total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total, value=done)
                    self.status_var.set(text if len(text) < 75 else "…" + text[-72:])
                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._set_busy(False)
                    self.status_var.set("Listo")
                    callback = self._on_done
                    self._on_done = None
                    if callback:
                        callback(message[1])
                elif kind == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._set_busy(False)
                    self.status_var.set("Error")
                    self._log(message[1])
                    messagebox.showerror("Error", message[1].splitlines()[-1] if message[1] else "Error inesperado")
        except queue.Empty:
            pass
        if self.busy:
            self.window.after(80, self._poll)

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.home()))
        if selected:
            self.root_var.set(selected)
            self.library_var.set(str(_library_default(Path(selected))))
            self.plan = None
            self.tracks = []
            self._clear_tree()
            self._log(f"Carpeta de origen: {selected}")
            self._log(f"Carpeta organizada: {self.library_var.get()}")
            self.analyze()

    def choose_library(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.library_var.get() or str(Path.home()))
        if selected:
            self.library_var.set(selected)
            self._log(f"Carpeta organizada: {selected}")
            self.analyze()

    def open_source(self) -> None:
        self._open_path(Path(self.root_var.get()))

    def open_library(self) -> None:
        self._open_path(Path(self.library_var.get()))

    @staticmethod
    def _open_path(path: Path) -> None:
        if not path.exists():
            messagebox.showerror("Carpeta no válida", f"No existe:\n{path}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("No se pudo abrir", str(exc))

    def _clear_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def analyze(self) -> None:
        if self.busy:
            return
        settings = self._settings()
        if settings is None:
            return

        def worker(progress, stop):
            tracks = scan_library(settings, progress, include_library=True)
            plan = build_plan(settings, source_tracks(settings, tracks))
            report = write_plan_report(settings, plan, "plan")
            return tracks, plan, report

        def done(result) -> None:
            self.tracks, self.plan, report = result
            self._populate_tree(self.plan)
            self._log("")
            self._show_plan_summary(self.plan)
            self._log(f"Biblioteca total: {len(self.tracks)} pistas (origen + organizada).")
            self._log(f"Informe CSV: {report}")
            self._log("Vista previa únicamente; no se ha modificado nada.")
            self.status_var.set("Análisis completado")

        self._start_worker("Analizando biblioteca…", worker, done)

    def _show_plan_summary(self, plan: OrganizePlan) -> None:
        overview = scan_overview(plan.tracks)
        self._log(
            f"Origen: {len(plan.tracks)} canciones, {plan.albums} álbumes destino, "
            f"{human_size(overview['total_size'])}."
        )
        self._log(f"Formatos: {_format_summary(plan.tracks)}")
        if plan.already_organized:
            self._log(
                f"El origen ya tiene estructura Artista/Álbum "
                f"({plan.organized_matches}/{len(plan.audio_items)} pistas)."
            )
            self._log("Se comparará cada canción con la carpeta organizada antes de moverla.")
            self.organized_var.set(
                f"Origen ya organizado: {plan.organized_matches}/{len(plan.audio_items)} pistas"
            )
        else:
            self.organized_var.set(
                f"Origen con estructura Artista/Álbum: "
                f"{plan.organized_matches}/{len(plan.audio_items)} pistas"
            )
        counts = plan.counts()
        for key, value in sorted(counts.items()):
            self._log(f"  {STATUS_LABELS.get(key, key)}: {value}")
        conflicts = plan.conflicts
        if conflicts:
            self._log(f"Conflictos en destino: {len(conflicts)}. Se pedirá decisión al aplicar.")
            for item in conflicts[:8]:
                title = item.track.title if item.track else item.source.name
                artist = item.track.display_artist if item.track else ""
                self._log(f"  - {artist} · {title}")
                self._log(f"    Origen:  {_media_text(item.source_info)}")
                self._log(f"    Destino: {_media_text(item.target_info)}")
            if len(conflicts) > 8:
                self._log(f"  … y {len(conflicts) - 8} conflictos más")
        duplicates = [
            item for item in plan.audio_items
            if item.status == "duplicate" and item.target_info is not None
        ]
        if duplicates:
            self._log(f"Canciones idénticas que ya están en el destino: {len(duplicates)}")
            for item in duplicates[:5]:
                title = item.track.title if item.track else item.source.name
                self._log(f"  - {title}")
                self._log(f"    Origen:  {_media_text(item.source_info)}")
                self._log(f"    Destino: {_media_text(item.target_info)}")

    def _populate_tree(self, plan: OrganizePlan) -> None:
        self._clear_tree()
        for item in plan.audio_items:
            if not item.track:
                continue
            status = STATUS_LABELS.get(item.status, item.status)
            tag = ""
            if item.status == "same":
                tag = "ok"
            elif item.status in {"conflict", "duplicate"}:
                tag = "warn"
            elif item.status in {"missing", "error"}:
                tag = "error"
            try:
                source = item.source.relative_to(plan.root)
            except ValueError:
                source = item.source
            try:
                target = item.target.relative_to(plan.library_root)
            except ValueError:
                target = item.target
            target_info = item.target_info
            if target_info is None and item.status == "same":
                target_info = item.source_info
            source_format = item.track.format.upper() if item.track.format else "—"
            source_size = human_size(item.track.size)
            target_format = target_info.format_label if target_info else "—"
            target_size = human_size(target_info.size) if target_info else "—"
            self.tree.insert("", "end", values=(
                status,
                item.track.display_artist,
                item.track.album,
                item.track.title,
                source_format,
                source_size,
                target_format,
                target_size,
                str(source),
                str(target),
            ), tags=(tag,) if tag else ())

    def _confirm_apply(self, text: str) -> bool:
        if self.simulate_var.get():
            return True
        return messagebox.askyesno("Confirmar", text)

    def _prepare_plan(self, settings: Settings, on_ready: Callable[[OrganizePlan, Path], None]) -> None:
        def worker(progress, stop):
            tracks = scan_library(settings, progress, include_library=True)
            plan = build_plan(settings, source_tracks(settings, tracks))
            report = write_plan_report(settings, plan, "plan")
            return tracks, plan, report

        def done(result) -> None:
            self.tracks, self.plan, report = result
            self._populate_tree(self.plan)
            self._show_plan_summary(self.plan)
            self._log(f"Biblioteca total: {len(self.tracks)} pistas (origen + organizada).")
            self._log(f"Informe CSV: {report}")
            on_ready(self.plan, report)

        self._start_worker("Preparando organización…", worker, done)

    def _resolve_conflicts(self, plan: OrganizePlan) -> dict[str, str] | None:
        conflicts = plan.conflicts
        if not conflicts or self.simulate_var.get():
            return {}
        decisions: dict[str, str] = {}
        apply_all: str | None = None
        for index, item in enumerate(conflicts, start=1):
            if apply_all:
                decisions[path_key(item.source)] = apply_all
                continue
            decision, all_flag = ConflictDialog(
                self.window, item, index, len(conflicts),
            ).show()
            if decision is None:
                return None
            decisions[path_key(item.source)] = decision
            if all_flag:
                apply_all = decision
        return decisions

    def _ensure_library(self, settings: Settings) -> bool:
        try:
            settings.library_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("No se pudo crear la carpeta", str(exc))
            return False
        return True

    def organize(self) -> None:
        if self.busy:
            return
        settings = self._settings()
        if settings is None:
            return
        simulate = self.simulate_var.get()

        def ready(plan: OrganizePlan, report: Path) -> None:
            changes = len(plan.changes)
            if not changes:
                messagebox.showinfo("Sin cambios", "No hay archivos que mover.")
                return
            if simulate:
                self._log("")
                self._log(f"Simulación: se moverían {changes} archivos.")
                self._log("No se ha modificado nada.")
                return
            decisions = self._resolve_conflicts(plan)
            if decisions is None:
                self._log("Organización cancelada.")
                return
            if not messagebox.askyesno(
                "Confirmar organización",
                f"Se moverán {changes} archivos al destino:\n{settings.library_root}\n\n"
                "¿Continuar?",
            ):
                self._log("Organización cancelada.")
                return
            if not self._ensure_library(settings):
                return

            def worker(progress, stop):
                summary = apply_plan(plan, settings, progress, stop, decisions=decisions)
                return summary, report

            def done(result) -> None:
                summary, report_path = result
                self._show_summary(summary)
                self._log(f"Informe: {report_path}")
                self.window.after(600, self.analyze)

            self._start_worker("Organizando…", worker, done)

        self._prepare_plan(settings, ready)

    def download_lyrics(self) -> None:
        if self.busy:
            return
        settings = self._settings()
        if settings is None:
            return
        if not self.tracks:
            messagebox.showinfo("Analiza primero", "Pulsa «Analizar» para leer la biblioteca.")
            return
        service = LyricsService(settings)
        missing = sum(1 for track in self.tracks if service.inspect(track) in {"would_fetch", "would_extract"})
        if not missing:
            messagebox.showinfo("Letras", "Todas las pistas ya tienen letras .lrc o incrustadas.")
            return
        if not self._confirm_apply(
            f"Se buscarán o extraerán letras para {missing} canciones.\n\n¿Continuar?"
        ):
            return
        simulate = self.simulate_var.get()
        overwrite = self.overwrite_lyrics_var.get()

        def worker(progress, stop):
            tracks = scan_library(settings, progress, include_library=True)
            service = LyricsService(settings)
            if simulate:
                return service.run(tracks, apply=False, progress=progress)
            return service.run(
                tracks, apply=True, overwrite=overwrite,
                progress=progress, stop_event=stop,
            )

        self._start_worker("Buscando letras…", worker, self._after_summary)

    def download_covers(self) -> None:
        if self.busy:
            return
        settings = self._settings()
        if settings is None:
            return
        if not self.tracks:
            messagebox.showinfo("Analiza primero", "Pulsa «Analizar» para leer la biblioteca.")
            return
        service = CoverService(settings)
        groups = _group_tracks(self.tracks)
        missing = sum(1 for group in groups if service.inspect(group) in {"would_fetch", "would_extract"})
        if not missing:
            messagebox.showinfo("Portadas", "Todos los álbumes ya tienen portada.")
            return
        if not self._confirm_apply(
            f"Se extraerán o descargarán portadas para {missing} álbumes.\n\n¿Continuar?"
        ):
            return
        simulate = self.simulate_var.get()
        overwrite = self.overwrite_covers_var.get()

        def worker(progress, stop):
            tracks = scan_library(settings, progress, include_library=True)
            service = CoverService(settings)
            if simulate:
                return service.run(tracks, apply=False, progress=progress)
            return service.run(
                tracks, apply=True, overwrite=overwrite,
                progress=progress, stop_event=stop,
            )

        self._start_worker("Buscando portadas…", worker, self._after_summary)

    def do_all(self) -> None:
        if self.busy:
            return
        settings = self._settings()
        if settings is None:
            return
        simulate = self.simulate_var.get()
        overwrite_covers = self.overwrite_covers_var.get()
        overwrite_lyrics = self.overwrite_lyrics_var.get()

        def ready(plan: OrganizePlan, report: Path) -> None:
            changes = len(plan.changes)
            if not simulate:
                action = (
                    f"Se organizarán {changes} archivos"
                    if changes else "No hay archivos que mover"
                )
                if not self._confirm_apply(
                    f"{action} y después se buscarán letras y portadas faltantes.\n\n"
                    "Cada etapa queda registrada en el diario; «Deshacer» revierte la última sesión.\n\n"
                    "¿Continuar?"
                ):
                    return
                decisions = self._resolve_conflicts(plan)
                if decisions is None:
                    self._log("Operación cancelada.")
                    return
                if not self._ensure_library(settings):
                    return

                def worker(progress, stop):
                    organize_summary = apply_plan(
                        plan, settings, progress, stop, decisions=decisions,
                    )
                    if organize_summary.errors:
                        return "organize_error", organize_summary, report
                    tracks = scan_library(settings, progress, include_library=True)
                    cover_summary = CoverService(settings).run(
                        tracks, apply=True, overwrite=overwrite_covers,
                        progress=progress, stop_event=stop,
                    )
                    lyrics_summary = LyricsService(settings).run(
                        tracks, apply=True, overwrite=overwrite_lyrics,
                        progress=progress, stop_event=stop,
                    )
                    return "all", organize_summary, cover_summary, lyrics_summary, report

                def done(result) -> None:
                    if result[0] == "organize_error":
                        _, summary, report_path = result
                        self._show_summary(summary)
                        self._log(f"Informe: {report_path}")
                        messagebox.showwarning(
                            "Organización incompleta",
                            "Hubo errores al mover archivos; no se descargaron letras ni portadas.",
                        )
                        self.window.after(600, self.analyze)
                        return
                    _, organize_summary, cover_summary, lyrics_summary, report_path = result
                    self._show_summary(organize_summary)
                    self._show_summary(cover_summary)
                    self._show_summary(lyrics_summary)
                    self._log(f"Informe: {report_path}")
                    self.window.after(600, self.analyze)

                self._start_worker("Organizando, portadas y letras…", worker, done)
                return

            def sim_worker(progress, stop):
                cover_summary = CoverService(settings).run(
                    self.tracks, apply=False, progress=progress,
                )
                lyrics_summary = LyricsService(settings).run(
                    self.tracks, apply=False, progress=progress,
                )
                return cover_summary, lyrics_summary

            def sim_done(result) -> None:
                cover_summary, lyrics_summary = result
                self._log("")
                self._log(f"Simulación: se moverían {changes} archivos.")
                self._show_summary(cover_summary)
                self._show_summary(lyrics_summary)
                self._log("No se ha modificado ni descargado nada.")

            self._start_worker("Simulando todo…", sim_worker, sim_done)

        self._prepare_plan(settings, ready)

    def undo(self) -> None:
        if self.busy:
            return
        settings = self._settings()
        if settings is None:
            return
        if not messagebox.askyesno(
            "Deshacer",
            "Se deshará la última sesión registrada (movimientos, letras o portadas).\n\n¿Continuar?",
        ):
            return

        def worker(progress, stop):
            return undo_last(settings, progress)

        def done(summary) -> None:
            self._show_summary(summary)
            self.window.after(500, self.analyze)

        self._start_worker("Deshaciendo…", worker, done)

    def stop_current(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        self.status_var.set("Deteniendo…")
        self._log("Solicitud de detención enviada; se terminará la tarea actual.")

    def _after_summary(self, summary: OperationSummary) -> None:
        self._show_summary(summary)

    def _show_summary(self, summary: OperationSummary) -> None:
        self._log("")
        self._log(f"Resultado: {summary.operation}")
        for status, count in sorted(summary.counts.items()):
            self._log(f"  {STATUS_LABELS.get(status, status)}: {count}")
        if summary.journal_path:
            self._log(f"Registro para deshacer: {summary.journal_path}")
        if summary.report_path:
            self._log(f"Informe: {summary.report_path}")
        for detail in summary.details[:20]:
            self._log(f"  - {detail}")
        if len(summary.details) > 20:
            self._log(f"  … y {len(summary.details) - 20} más")

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Salir", "Hay una operación en curso. ¿Detenerla y salir?"):
                return
            self.stop_event.set()
        self.window.destroy()


def main() -> None:
    window = Tk()
    OrganizerApp(window)
    window.mainloop()


if __name__ == "__main__":
    main()
