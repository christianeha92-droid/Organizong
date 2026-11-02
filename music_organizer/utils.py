from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".aac", ".aiff", ".aif", ".wav", ".ogg", ".opus", ".wma",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
LYRICS_EXTENSIONS = {".lrc"}

_FORBIDDEN_MAP = {
    "/": "-",
    "\\": "-",
    ":": " -",
    "?": "",
    "*": "",
    '"': "'",
    "<": "",
    ">": "",
    "|": "-",
}
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_EDITION_RE = re.compile(
    r"\b("
    r"deluxe|edition|edici[oó]n|remaster(?:ed)?|remasterizado|anniversary|aniversario|"
    r"expanded|expandida|special|especial|bonus|bonus track|limited|limitada|"
    r"collector'?s|coleccionista|reissue|reedici[oó]n|mono|stereo"
    r")\b",
    re.IGNORECASE,
)
_FEATURING_RE = re.compile(
    r"\s*(?:,|\s+)(?:feat\.?|ft\.?|featuring|con)\s+.*$", re.IGNORECASE
)
_BRACKET_RE = re.compile(r"[\(\[\{].*?[\)\]\}]")
_GENERIC_FOLDERS = {
    "music", "musica", "música", "musique", "audio", "audios", "canciones",
    "songs", "downloads", "descargas", "mi musica", "mi música", "música en español",
    "musica en español", "música japonesa", "musica japonesa", "musica hindú",
    "música hindú", "rock peruano", "español", "espanol", "varios", "various artists",
    "telegram", "nueva carpeta", "new folder",
}


def normalize_text(value: object | None) -> str:
    """Normaliza texto Unicode y espacios sin cambiar el contenido artístico."""
    if value is None:
        return ""
    text = str(value).replace("\x00", " ")
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    cleaned = []
    for char in text:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf"} and char not in "\t\n\r":
            cleaned.append(" ")
        else:
            cleaned.append(char)
    text = "".join(cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_match(value: object | None) -> str:
    """Forma agresiva para comparar títulos/artistas, sin acentos ni puntuación."""
    text = normalize_text(value).casefold()
    text = text.replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _BRACKET_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_album_match(value: object | None) -> str:
    text = normalize_match(value)
    text = _EDITION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_component(value: object | None, fallback: str = "Sin nombre", max_len: int = 90) -> str:
    """Convierte un nombre de etiqueta en un nombre de carpeta válido en Windows."""
    text = normalize_text(value)
    for char, replacement in _FORBIDDEN_MAP.items():
        text = text.replace(char, replacement)
    text = re.sub(r"\s*-\s*(?:-\s*)+", " - ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .-")
    return text or fallback


def sanitize_filename(title: object | None, extension: str, max_len: int = 110) -> str:
    ext = extension.lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    stem = sanitize_component(title, "Sin título", max_len=max(10, max_len - len(ext)))
    return f"{stem}{ext}"


def parse_int(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
        if isinstance(value, tuple):
            value = value[0]
    text = str(value)
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def parse_number_pair(value: object | None) -> tuple[int | None, int | None]:
    """Devuelve (número, total) desde '3/12' o desde una tupla MP4."""
    if value is None:
        return None, None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, tuple):
            number = first[0] if len(first) > 0 else None
            total = first[1] if len(first) > 1 else None
            return int(number) if number else None, int(total) if total else None
    text = str(value)
    match = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def infer_title_from_filename(path: Path) -> str:
    stem = normalize_text(path.stem).replace("_", " ")
    stem = re.sub(r"^\s*(?:cd|disc|disk)\s*\d+\s*[-._ ]+", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"^\s*\d{1,3}\s*[-._ )]+\s*", "", stem)
    stem = re.sub(r"^\s*\d{1,3}\s+", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -._")
    return stem or normalize_text(path.stem) or "Sin título"


def format_track_filename(
    title: str,
    extension: str,
    track_number: int | None,
    disc_number: int | None,
    multi_disc: bool,
) -> str:
    clean_title = sanitize_component(title, "Sin título", max_len=105)
    prefix = ""
    if track_number:
        if multi_disc:
            prefix = f"{disc_number or 1}-{track_number:02d} - "
        else:
            prefix = f"{track_number:02d} - "
    return sanitize_filename(f"{prefix}{clean_title}", extension, max_len=120)


def fit_path(root: Path, artist: str, album: str, filename: str, max_path: int = 240) -> Path:
    """Recorta componentes si la ruta completa se acerca al límite de Windows."""
    artist = sanitize_component(artist, "Unknown Artist", 70)
    album = sanitize_component(album, "Unknown Album", 85)
    suffix = Path(filename).suffix or ".flac"
    stem = sanitize_component(Path(filename).stem, "Sin título", 115)

    def build(a: str, al: str, name: str) -> Path:
        return root / a / al / name

    candidate = build(artist, album, f"{stem}{suffix}")
    if len(str(candidate)) <= max_path:
        return candidate

    while len(str(build(artist, album, f"{stem}{suffix}"))) > max_path and len(stem) > 30:
        stem = stem[:-5].rstrip(" .-")
    candidate = build(artist, album, f"{stem}{suffix}")
    if len(str(candidate)) <= max_path:
        return candidate

    while len(str(build(artist, album, f"{stem}{suffix}"))) > max_path and len(album) > 25:
        album = album[:-5].rstrip(" .-")
    candidate = build(artist, album, f"{stem}{suffix}")
    if len(str(candidate)) <= max_path:
        return candidate

    while len(str(build(artist, album, f"{stem}{suffix}"))) > max_path and len(artist) > 20:
        artist = artist[:-5].rstrip(" .-")
    candidate = build(artist, album, f"{stem}{suffix}")
    return candidate


def path_key(path: Path | str) -> str:
    """Clave insensible a mayúsculas para comparar rutas (comportamiento Windows)."""
    text = str(path).replace("\\", "/")
    return os.path.normpath(text).casefold()


def text_similarity(left: object | None, right: object | None) -> float:
    a = normalize_match(left)
    b = normalize_match(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if a_tokens and b_tokens:
        common = a_tokens & b_tokens
        if common:
            jaccard = len(common) / len(a_tokens | b_tokens)
            containment = len(common) / min(len(a_tokens), len(b_tokens))
            token_score = max(jaccard, containment * 0.96)
        else:
            token_score = 0.0
    else:
        token_score = 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    return max(token_score, sequence)


def artist_similarity(left: object | None, right: object | None) -> float:
    left_text = normalize_text(left)
    right_text = normalize_text(right)
    candidates_left = [left_text]
    candidates_right = [right_text]
    for value, target in ((left_text, candidates_left), (right_text, candidates_right)):
        cleaned = _FEATURING_RE.sub("", value).strip()
        if cleaned and cleaned not in target:
            target.append(cleaned)
        for part in re.split(r"\s*(?:,|;|&|\+|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b)\s*", value, flags=re.IGNORECASE):
            part = part.strip()
            if part and part not in target:
                target.append(part)
    return max(
        (text_similarity(a, b) for a in candidates_left for b in candidates_right),
        default=0.0,
    )


def album_similarity(left: object | None, right: object | None) -> float:
    a = normalize_album_match(left)
    b = normalize_album_match(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return text_similarity(a, b)


def album_match_score(
    artist: str,
    album: str,
    candidate_artist: str,
    candidate_album: str,
    various_artists: bool = False,
) -> float:
    album_score = album_similarity(album, candidate_album)
    artist_score = 1.0 if various_artists else artist_similarity(artist, candidate_artist)
    if not normalize_match(artist):
        artist_score = 0.7
    return (album_score * 0.72) + (artist_score * 0.28)


def is_generic_folder(name: str) -> bool:
    return normalize_match(name) in {normalize_match(item) for item in _GENERIC_FOLDERS}


def format_duration(seconds: float | None) -> str:
    if not seconds or not math.isfinite(seconds) or seconds <= 0:
        return ""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_size(size: float | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.part")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def iter_audio_files(
    root: Path,
    ignore_dirs: set[Path] | None = None,
    exclude_dirs: set[Path] | None = None,
) -> list[Path]:
    ignored = {path_key(path) for path in (ignore_dirs or set())}
    excluded = {path_key(path) for path in (exclude_dirs or set())}
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        parents = [path.parent, *path.parents]
        if any(path_key(parent) in ignored for parent in parents):
            continue
        if any(path_key(parent) in excluded for parent in parents):
            continue
        result.append(path)
    return sorted(result, key=lambda item: str(item).casefold())


def remove_empty_dirs(root: Path, protected: set[Path] | None = None) -> list[Path]:
    protected_keys = {path_key(path) for path in (protected or set())}
    removed: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        if path_key(path) in protected_keys:
            continue
        if any(path_key(parent) in protected_keys for parent in path.parents):
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            try:
                path.rmdir()
                removed.append(path)
            except OSError:
                pass
        except OSError:
            pass
    return removed


def copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.part")
    shutil.copy2(source, temp)
    os.replace(temp, target)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
