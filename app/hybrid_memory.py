#!/usr/bin/env python3
"""Расширенная память для Codex — движок и командная строка.

Заметки лежат в markdown (MEMORY.md + папка memory/). Над ними строится локальный
гибридный индекс: полнотекстовый SQLite FTS5 + векторы эмбеддингов (Ollama) +
переранжирование. Только стандартная библиотека Python 3.8+, всё хранится локально.

Запуск: memory <команда> (обёртка ~/.codex-memory/bin/memory) или
        python3 hybrid_memory.py <команда>. Список команд: memory --help
"""
from __future__ import annotations

import argparse
import array
import datetime as dt
import fnmatch
import hashlib
import json
import operator
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # i18n.py лежит рядом: путь нужен, когда движок импортируют из другой папки
    sys.path.insert(0, _HERE)

import i18n  # noqa: E402
from i18n import L  # noqa: E402

VERSION = "1.2.0"
SERVER_NAME = "extended-memory"


def _resolve_home() -> Path:
    env = os.environ.get("CODEX_MEMORY_HOME", "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    return Path(__file__).resolve().parents[1]


HOME_DIR = _resolve_home()
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = HOME_DIR / "index-roots.json"
MEMORY_FILE = HOME_DIR / "MEMORY.md"
NOTES_DIR = HOME_DIR / "memory"
AUTO_DIR = NOTES_DIR / "auto"
HISTORY_DIR = NOTES_DIR / "history"
INDEX_DIR = HOME_DIR / ".index"
DB_PATH = INDEX_DIR / "index.sqlite"
LOCK_DIR = INDEX_DIR / "sync.lock"
STATE_DIR = HOME_DIR / ".state"
LOG_DIR = HOME_DIR / "logs"

CHUNK_LINES = 24
CHUNK_OVERLAP = 6
DEFAULT_MAX_FILE_BYTES = 800_000
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/embed"
DEFAULT_MODEL = "bge-m3"
LOCK_STALE_SECONDS = 2 * 60 * 60
LOCK_HARD_STALE_SECONDS = 24 * 60 * 60  # старше суток замок чужой: после перезагрузки его pid мог достаться другому процессу
SCHEMA_VERSION = "2"
# Раздел MEMORY.md для новых фактов: находится по любому из двух заголовков, создаётся на текущем языке.
CURATED_SECTIONS = ("## Активная память", "## Active memory")
MEMORY_CONTEXT_LIMIT = 9000  # столько символов MEMORY.md уходит в начало каждой сессии
DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".cache", ".idea", ".vscode", ".obsidian", ".trash", ".Trash", "vendor", "coverage",
}
CATEGORY_LABELS = {
    "preference": L("предпочтение", "preference"),
    "workflow": L("правило", "rule"),
    "project": L("проект", "project"),
    "infrastructure": L("инфраструктура", "infrastructure"),
    "decision": L("решение", "decision"),
    "person": L("люди", "people"),
    "other": L("заметка", "note"),
}


def curated_section() -> str:
    """Заголовок раздела для новых фактов на текущем языке."""
    return CURATED_SECTIONS[1] if i18n.current() == "en" else CURATED_SECTIONS[0]


# ---------------------------------------------------------------- утилиты

def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fix_stdio() -> None:
    """Windows: вывод в трубу или файл идёт в кодировке системы (cp1251) и падает на «✓» — переводим в UTF-8."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass  # у pythonw.exe потоков вывода нет вовсе


def now_local() -> dt.datetime:
    return dt.datetime.now()


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def replace_file(tmp: Path, path: Path) -> None:
    """os.replace с повтором: на Windows файл на мгновение может держать другой процесс (антивирус, читатель)."""
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    replace_file(tmp, path)


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(text)


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def one_line(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def rel_to_home(path: Path) -> str:
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- секреты

SECRET_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"), "[REDACTED]"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{16,})\b"), "[REDACTED]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{16,})\b"), "[REDACTED]"),
    (re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED]"),
    (re.compile(r"\b(glpat-[A-Za-z0-9_-]{16,})\b"), "[REDACTED]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED]"),
    (re.compile(r"\b(AIza[0-9A-Za-z_-]{30,})\b"), "[REDACTED]"),
    (re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{35})\b"), "[REDACTED]"),
    (re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I), "[REDACTED]"),
    (re.compile(r"\b([A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})\b"), "[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}", re.I), r"\1[REDACTED]"),
    (
        re.compile(
            r"((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd|authorization|пароль|токен)\s*[:=]\s*)"
            r"(\"[^\"]+\"|'[^']+'|`[^`]+`|[^\s,;]+)",
            re.I,
        ),
        r"\1[REDACTED]",
    ),
]


def redact(text: str) -> str:
    result = str(text or "")
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# ---------------------------------------------------------------- настройки

def default_config() -> Dict[str, Any]:
    return {
        "schemaVersion": 1,
        "includeExts": [".md", ".txt"],
        "maxFileBytes": DEFAULT_MAX_FILE_BYTES,
        "roots": [
            {"path": "MEMORY.md", "logicalPrefix": "MEMORY.md"},
            {"path": "memory", "logicalPrefix": "memory"},
        ],
        "embedding": {
            "enabled": True,
            "provider": "ollama",
            "endpoint": DEFAULT_ENDPOINT,
            "model": DEFAULT_MODEL,
            "batchSize": 16,
            "maxChars": 2500,
            "timeoutSeconds": 180,
            "keepAlive": "2m",
        },
        "search": {"ftsCandidates": 64, "vectorCandidates": 64},
        "rerank": {"vectorWeight": 0.55, "ftsWeight": 0.2, "tokenWeight": 0.15, "pathWeight": 0.1},
    }


def load_config() -> Dict[str, Any]:
    config = default_config()
    if not CONFIG_PATH.exists():
        return config
    try:
        data = json.loads(read_text(CONFIG_PATH))
    except Exception as exc:  # битый JSON не должен ломать поиск
        warn(L(
            f"не удалось прочитать {CONFIG_PATH}: {exc}; использую настройки по умолчанию",
            f"could not read {CONFIG_PATH}: {exc}; using the default settings",
        ))
        return config
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            merged = dict(config[key])
            merged.update(value)
            config[key] = merged
        else:
            config[key] = value
    return config


def save_config(config: Dict[str, Any]) -> None:
    atomic_write(CONFIG_PATH, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o644)


@dataclass
class RootSpec:
    abs_path: Path
    logical_prefix: str
    include_exts: Set[str]
    exclude_dirs: Set[str]
    exclude_globs: List[str]
    max_depth: Optional[int]
    max_file_bytes: int
    include_hidden: bool


def expand_path(value: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute():
        path = HOME_DIR / path
    return path.resolve()


def build_root_specs(config: Dict[str, Any]) -> List[RootSpec]:
    include_exts = {str(ext).lower() for ext in config.get("includeExts", [".md", ".txt"])} or {".md"}
    max_file_bytes = int(config.get("maxFileBytes", DEFAULT_MAX_FILE_BYTES))
    specs: List[RootSpec] = []
    for raw in config.get("roots", []):
        if not isinstance(raw, dict) or not raw.get("path"):
            continue
        specs.append(
            RootSpec(
                abs_path=expand_path(str(raw["path"])),
                logical_prefix=str(raw.get("logicalPrefix", "")).strip().strip("/"),
                include_exts={str(ext).lower() for ext in raw.get("includeExts", include_exts)} or include_exts,
                exclude_dirs=DEFAULT_EXCLUDE_DIRS | {str(item) for item in raw.get("excludeDirs", [])},
                exclude_globs=[str(item) for item in raw.get("excludeGlobs", [])],
                max_depth=int(raw["maxDepth"]) if raw.get("maxDepth") is not None else None,
                max_file_bytes=int(raw.get("maxFileBytes", max_file_bytes)),
                include_hidden=bool(raw.get("includeHidden", False)),
            )
        )
    return specs


def _skip_by_glob(spec: RootSpec, rel_posix: str, abs_path: Path) -> bool:
    for pattern in spec.exclude_globs:
        if fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(abs_path.name, pattern):
            return True
    return False


def _logical_path(spec: RootSpec, abs_path: Path) -> str:
    if spec.abs_path.is_file():
        return spec.logical_prefix or abs_path.name
    rel = abs_path.relative_to(spec.abs_path).as_posix()
    return f"{spec.logical_prefix}/{rel}" if spec.logical_prefix else rel


def iter_index_files(config: Dict[str, Any]) -> Iterator[Tuple[str, Path]]:
    seen: Set[str] = set()
    for spec in build_root_specs(config):
        if not spec.abs_path.exists():
            continue
        if spec.abs_path.is_file():
            candidates = [spec.abs_path]
            walker: Iterator[Tuple[str, List[str], List[str]]] = iter(())
        else:
            candidates = []
            walker = os.walk(spec.abs_path)
        for dirpath, dirnames, filenames in walker:
            current = Path(dirpath)
            rel_dir = current.relative_to(spec.abs_path)
            depth = 0 if str(rel_dir) == "." else len(rel_dir.parts)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in spec.exclude_dirs
                and (spec.include_hidden or not name.startswith("."))
                and not _skip_by_glob(spec, (rel_dir / name).as_posix(), current / name)
            )
            if spec.max_depth is not None and depth >= spec.max_depth:
                dirnames[:] = []
            for filename in sorted(filenames):
                if not spec.include_hidden and filename.startswith("."):
                    continue
                candidates.append(current / filename)
        for abs_path in candidates:
            if abs_path.suffix.lower() not in spec.include_exts:
                continue
            rel_posix = abs_path.name if spec.abs_path.is_file() else abs_path.relative_to(spec.abs_path).as_posix()
            if _skip_by_glob(spec, rel_posix, abs_path):
                continue
            try:
                if abs_path.stat().st_size > spec.max_file_bytes:
                    continue
            except OSError:
                continue
            logical = _logical_path(spec, abs_path)
            if logical in seen:
                continue
            seen.add(logical)
            yield logical, abs_path


# ---------------------------------------------------------------- индекс

def connect() -> sqlite3.Connection:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if not row or row["value"] != SCHEMA_VERSION:
        conn.executescript(
            "DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS embeddings; DROP TABLE IF EXISTS file_state;"
        )
        conn.execute("REPLACE INTO meta(key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS file_state (
             path TEXT PRIMARY KEY, abs_path TEXT NOT NULL, mtime REAL NOT NULL,
             size INTEGER NOT NULL, sha1 TEXT NOT NULL, indexed_at REAL NOT NULL)"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
             chunk_key UNINDEXED, path UNINDEXED, abs_path UNINDEXED, heading,
             start_line UNINDEXED, end_line UNINDEXED, text,
             tokenize='porter unicode61 remove_diacritics 2 tokenchars ''-_.:@/''')"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS embeddings (
             chunk_key TEXT PRIMARY KEY, path TEXT NOT NULL, model TEXT NOT NULL,
             dims INTEGER NOT NULL, vector BLOB NOT NULL, updated_at REAL NOT NULL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_path ON embeddings(path)")
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row and row["value"] != "" else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


class SyncBusy(Exception):
    """Индекс уже обновляется другим процессом."""


def _lock_pid() -> Optional[int]:
    try:
        return int((LOCK_DIR / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive_windows(pid: int) -> bool:
    """Жив ли процесс. os.kill(pid, 0) на Windows не годится: он не проверяет процесс, а завершает его."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: процесс есть, но чужой
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except Exception:
            return True  # не смогли проверить — считаем живым; через сутки замок снимется по возрасту
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _remove_lock() -> None:
    try:
        for child in LOCK_DIR.iterdir():
            child.unlink()
        LOCK_DIR.rmdir()
    except OSError:
        pass


@contextmanager
def sync_lock() -> Iterator[None]:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            LOCK_DIR.mkdir()
            break
        except FileExistsError:
            pid = _lock_pid()
            try:
                age = time.time() - LOCK_DIR.stat().st_mtime
            except OSError:
                age = 0.0
            stale = (
                (pid is not None and not _pid_alive(pid))
                or (pid is None and age > LOCK_STALE_SECONDS)
                or age > LOCK_HARD_STALE_SECONDS
            )
            if not stale:
                raise SyncBusy()
            _remove_lock()
    else:
        raise SyncBusy()
    (LOCK_DIR / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        yield
    finally:
        if _lock_pid() in (None, os.getpid()):
            _remove_lock()


# ---------------------------------------------------------------- нарезка

_heading_re = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
_blank_re = re.compile(r"^\s*$")


@dataclass
class Chunk:
    chunk_key: str
    path: str
    abs_path: str
    heading: str
    start_line: int
    end_line: int
    text: str


def _chunk(path: str, abs_path: str, heading: str, start: int, end: int, text: str) -> Chunk:
    key = sha1_text(f"{path}:{start}:{end}:{sha1_text(text)}")
    return Chunk(key, path, abs_path, heading, start, end, text)


def _sections(lines: Sequence[str]) -> List[Tuple[int, int, str, List[str]]]:
    sections: List[Tuple[int, int, str, List[str]]] = []
    heading, start, current = "", 1, []  # type: str, int, List[str]
    for idx, line in enumerate(lines, start=1):
        match = _heading_re.match(line)
        if match and current:
            sections.append((start, idx - 1, heading, current))
            heading, start, current = match.group(2).strip(), idx, [line]
            continue
        if match and not current:
            heading, start = match.group(2).strip(), idx
        current.append(line)
    sections.append((start, len(lines), heading, current))
    return sections


_bullet_re = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+\S")
_comment_re = re.compile(r"<!--[\s\S]*?-->")
_wordy_re = re.compile(r"\w\w")
_stamped_re = re.compile(r"^\s*- (\*\*\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2} ·)")
MAX_PARA_CHARS = 1200    # абзацы склеиваются в фрагменты до такого размера
MAX_BLOCK_CHARS = 2400   # длиннее — режем окнами строк
TINY_BULLET_CHARS = 60    # совсем короткие пункты («- молоко») склеиваются…
SMALL_BULLETS_CHARS = 160  # …в фрагменты до такого размера; обычный факт всегда отдельно


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _blocks(lines: Sequence[str]) -> List[Tuple[int, int, bool]]:
    """Пункты списка (с вложенными строками) и абзацы: (начало, конец включительно, это_пункт)."""
    blocks: List[Tuple[int, int, bool]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _blank_re.match(line) or _heading_re.match(line):
            i += 1
            continue
        bullet = _bullet_re.match(line)
        j = i + 1
        if bullet:
            base = len(bullet.group(1).expandtabs(4))
            while j < n:
                nxt = lines[j]
                if _heading_re.match(nxt):
                    break
                if _blank_re.match(nxt):
                    k = j + 1  # пустая строка внутри пункта, если дальше идёт отступ
                    while k < n and _blank_re.match(lines[k]):
                        k += 1
                    if k < n and _indent(lines[k].expandtabs(4)) > base and not _heading_re.match(lines[k]):
                        j = k
                        continue
                    break
                if _indent(nxt.expandtabs(4)) <= base and (_bullet_re.match(nxt) or _indent(nxt.expandtabs(4)) == 0):
                    break
                j += 1
        else:
            while j < n and not _blank_re.match(lines[j]) and not _heading_re.match(lines[j]) and not _bullet_re.match(lines[j]):
                j += 1
        end = j - 1
        while end > i and _blank_re.match(lines[end]):
            end -= 1
        blocks.append((i, end, bool(bullet)))
        i = j
    return blocks


def make_chunks(path: str, abs_path: str, text: str) -> List[Chunk]:
    """Фрагменты для индекса: пункт списка = один фрагмент (так факты не смешиваются), абзацы — до 1200 символов."""
    text = _comment_re.sub(lambda m: "\n" * m.group(0).count("\n"), text)  # служебные комментарии не ищутся
    lines = text.splitlines()
    if not lines:
        return []
    chunks: List[Chunk] = []

    def emit(heading: str, first: int, last: int) -> None:
        body = "\n".join(lines[first:last + 1]).strip()
        if not _wordy_re.search(body):
            return
        if len(body) <= MAX_BLOCK_CHARS:
            chunks.append(_chunk(path, abs_path, heading, first + 1, last + 1, body))
            return
        step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
        for w in range(first, last + 1, step):
            w_end = min(last, w + CHUNK_LINES - 1)
            piece = "\n".join(lines[w:w_end + 1]).strip()
            if _wordy_re.search(piece):
                chunks.append(_chunk(path, abs_path, heading, w + 1, w_end + 1, piece))
            if w_end >= last:
                break

    for sec_start, sec_end, heading, _ in _sections(lines):
        sec_lines = lines[sec_start - 1:sec_end]
        pending: Optional[List[Any]] = None  # [начало, конец, вид, символов]
        for b_start, b_end, is_bullet in _blocks(sec_lines):
            first, last = sec_start - 1 + b_start, sec_start - 1 + b_end
            size = sum(len(lines[k]) + 1 for k in range(first, last + 1))
            stamped = _stamped_re.match(lines[first])  # записи памяти (с датой/временем) — всегда отдельно
            kind = ("tiny" if size <= TINY_BULLET_CHARS and not stamped else "bullet") if is_bullet else "para"
            if pending and kind == pending[2] and (
                (kind == "tiny" and pending[3] + size <= SMALL_BULLETS_CHARS)
                or (kind == "para" and pending[3] + size <= MAX_PARA_CHARS)
            ):
                pending[1], pending[3] = last, pending[3] + size
                continue
            if pending:
                emit(heading, pending[0], pending[1])
            pending = [first, last, kind, size]
        if pending:
            emit(heading, pending[0], pending[1])
    return chunks


# ---------------------------------------------------------------- эмбеддинги

class EmbedderUnavailable(Exception):
    pass


class Embedder:
    """Клиент эмбеддингов: Ollama (/api/embed) или OpenAI-совместимый /v1/embeddings."""

    def __init__(self, config: Dict[str, Any], timeout: Optional[int] = None) -> None:
        emb = config.get("embedding") or {}
        self.enabled = bool(emb.get("enabled", False))
        self.endpoint = str(emb.get("endpoint") or DEFAULT_ENDPOINT)
        self.model = str(emb.get("model") or DEFAULT_MODEL)
        self.batch_size = max(1, int(emb.get("batchSize", 16)))
        self.max_chars = max(256, int(emb.get("maxChars", 2500)))
        self.timeout = timeout or max(5, int(emb.get("timeoutSeconds", 180)))
        self.keep_alive = emb.get("keepAlive")
        self.api_key_env = emb.get("apiKeyEnv")
        self.available = self.enabled
        self.last_error = ""
        host = (urllib.parse.urlparse(self.endpoint).hostname or "").lower()
        handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost", "::1") else []
        self._opener = urllib.request.build_opener(*handlers)

    def _request(self, texts: Sequence[str]) -> List[List[float]]:
        payload: Dict[str, Any] = {"model": self.model, "input": list(texts)}
        if self.keep_alive and "/api/" in self.endpoint:
            payload["keep_alive"] = str(self.keep_alive)
        headers = {"Content-Type": "application/json"}
        if self.api_key_env and os.environ.get(str(self.api_key_env)):
            headers["Authorization"] = f"Bearer {os.environ[str(self.api_key_env)]}"
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")[:300]
            if exc.code == 404 or "not found" in body.lower():
                raise EmbedderUnavailable(L(
                    f"модель {self.model} не найдена ({body.strip() or exc.code}); выполните: ollama pull {self.model}",
                    f"model {self.model} not found ({body.strip() or exc.code}); run: ollama pull {self.model}",
                ))
            raise RuntimeError(f"HTTP {exc.code}: {body.strip()}")
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise EmbedderUnavailable(L(
                f"сервер эмбеддингов {self.endpoint} не отвечает: {exc}",
                f"embedding server {self.endpoint} is not responding: {exc}",
            ))
        if isinstance(data, dict) and data.get("embeddings") is not None:
            return data["embeddings"]
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [item["embedding"] for item in sorted(data["data"], key=lambda it: it.get("index", 0))]
        if isinstance(data, dict) and data.get("embedding") is not None:
            return [data["embedding"]]
        raise RuntimeError(L("в ответе нет эмбеддингов", "no embeddings in the response"))

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.enabled:
            raise EmbedderUnavailable(L("эмбеддинги выключены", "embeddings are off"))
        if not self.available:
            raise EmbedderUnavailable(self.last_error or L("сервер эмбеддингов недоступен", "embedding server is unavailable"))
        try:
            vectors = self._request(texts)
        except EmbedderUnavailable as exc:
            self.available = False
            self.last_error = str(exc)
            raise
        if len(vectors) != len(texts):
            raise RuntimeError(L(
                f"ожидалось {len(texts)} векторов, пришло {len(vectors)}",
                f"expected {len(texts)} vectors, got {len(vectors)}",
            ))
        return vectors


def normalize_vector(values: Sequence[float]) -> List[float]:
    norm = sum(v * v for v in values) ** 0.5
    if norm == 0.0:
        return [0.0 for _ in values]
    return [v / norm for v in values]


def vector_to_blob(values: Sequence[float]) -> bytes:
    return array.array("f", values).tobytes()


def blob_to_vector(blob: bytes) -> "array.array[float]":
    vec = array.array("f")
    vec.frombytes(blob)
    return vec


def embed_missing(
    conn: sqlite3.Connection,
    embedder: Embedder,
    paths: Optional[Set[str]] = None,
    deadline: Optional[float] = None,
    quiet: bool = True,
) -> Tuple[int, int, bool]:
    """Досчитывает векторы для фрагментов без них. Возвращает (посчитано, ошибок, всё_готово)."""
    rows = conn.execute(
        """SELECT c.chunk_key, c.path, c.heading, c.text FROM chunks c
           LEFT JOIN embeddings e ON e.chunk_key = c.chunk_key AND e.model = ?
           WHERE e.chunk_key IS NULL
           ORDER BY CASE WHEN c.path = 'MEMORY.md' THEN 0
                         WHEN c.path LIKE 'memory/auto/%' THEN 2
                         WHEN c.path LIKE 'memory/history/%' THEN 3
                         WHEN c.path LIKE 'memory/%' THEN 1 ELSE 2 END,
                    c.path DESC, CAST(c.start_line AS INTEGER)""",
        (embedder.model,),
    ).fetchall()
    if paths is not None:
        rows = [row for row in rows if row["path"] in paths]
    done = failed = 0
    for start in range(0, len(rows), embedder.batch_size):
        if deadline is not None and time.time() > deadline:
            return done, failed, False
        batch = rows[start:start + embedder.batch_size]
        inputs = []
        for row in batch:
            heading = f"{row['heading']}\n" if row["heading"] else ""
            inputs.append(f"{row['path']}\n{heading}{row['text']}".strip()[: embedder.max_chars])
        try:
            vectors = embedder.embed(inputs)
            pairs = list(zip(batch, vectors))
        except EmbedderUnavailable as exc:
            meta_set(conn, "last_embedding_error", str(exc))
            conn.commit()
            return done, failed, False
        except Exception:
            pairs = []
            for row, text in zip(batch, inputs):  # по одному, чтобы найти «плохой» фрагмент
                try:
                    pairs.append((row, embedder.embed([text])[0]))
                except EmbedderUnavailable as exc:
                    meta_set(conn, "last_embedding_error", str(exc))
                    conn.commit()
                    return done, failed, False
                except Exception as exc:
                    failed += 1
                    meta_set(conn, "last_embedding_error", f"{row['path']}: {exc}")
        for row, vector in pairs:
            unit = normalize_vector(vector)
            conn.execute(
                "REPLACE INTO embeddings(chunk_key, path, model, dims, vector, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["chunk_key"], row["path"], embedder.model, len(unit), vector_to_blob(unit), time.time()),
            )
            done += 1
        conn.commit()
        if not quiet:
            print(L(f"  векторы: {done}/{len(rows)}", f"  vectors: {done}/{len(rows)}"), flush=True)
    if not failed:
        meta_set(conn, "last_embedding_error", "")
    meta_set(conn, "last_embedding_ok_at", str(time.time()))
    conn.commit()
    return done, failed, True


# ---------------------------------------------------------------- синхронизация

def _index_file(conn: sqlite3.Connection, logical: str, abs_path: Path) -> bool:
    """Обновляет текстовую часть индекса для файла. True — если что-то поменялось."""
    try:
        stat = abs_path.stat()
    except OSError:
        return False
    row = conn.execute("SELECT mtime, size, sha1 FROM file_state WHERE path = ?", (logical,)).fetchone()
    if row and float(row["mtime"]) == float(stat.st_mtime) and int(row["size"]) == int(stat.st_size):
        return False
    text = read_text(abs_path)
    digest = sha1_text(text)
    if row and row["sha1"] == digest:
        conn.execute(
            "UPDATE file_state SET mtime = ?, size = ?, abs_path = ? WHERE path = ?",
            (float(stat.st_mtime), int(stat.st_size), str(abs_path), logical),
        )
        return False
    new_chunks = make_chunks(logical, str(abs_path), text)
    new_keys = {chunk.chunk_key for chunk in new_chunks}
    conn.execute("DELETE FROM chunks WHERE path = ?", (logical,))
    conn.executemany(
        "INSERT INTO chunks(chunk_key, path, abs_path, heading, start_line, end_line, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(c.chunk_key, c.path, c.abs_path, c.heading, str(c.start_line), str(c.end_line), c.text) for c in new_chunks],
    )
    # векторы неизменившихся фрагментов переиспользуем, лишние — удаляем
    old_keys = [r["chunk_key"] for r in conn.execute("SELECT chunk_key FROM embeddings WHERE path = ?", (logical,))]
    stale = [(key,) for key in old_keys if key not in new_keys]
    if stale:
        conn.executemany("DELETE FROM embeddings WHERE chunk_key = ?", stale)
    conn.execute(
        "REPLACE INTO file_state(path, abs_path, mtime, size, sha1, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (logical, str(abs_path), float(stat.st_mtime), int(stat.st_size), digest, time.time()),
    )
    return True


def _remove_path(conn: sqlite3.Connection, logical: str) -> None:
    conn.execute("DELETE FROM file_state WHERE path = ?", (logical,))
    conn.execute("DELETE FROM chunks WHERE path = ?", (logical,))
    conn.execute("DELETE FROM embeddings WHERE path = ?", (logical,))


def sync(
    config: Optional[Dict[str, Any]] = None,
    only: Optional[Sequence[Path]] = None,
    text_only: bool = False,
    max_seconds: Optional[float] = None,
    quiet: bool = True,
) -> Dict[str, Any]:
    """Обновляет индекс. only — обновить лишь эти файлы (быстро, после записи заметки)."""
    config = config or load_config()
    started = time.time()
    with sync_lock():
        conn = connect()
        try:
            embedder = Embedder(config)
            if embedder.enabled:
                conn.execute("DELETE FROM embeddings WHERE model != ?", (embedder.model,))
            files = list(iter_index_files(config))
            targets: Optional[Set[str]] = None
            if only is not None:
                wanted = {Path(p).resolve() for p in only}
                files = [(logical, path) for logical, path in files if path.resolve() in wanted]
                targets = {logical for logical, _ in files}
            updated = removed = 0
            current = {logical for logical, _ in files}
            for logical, abs_path in files:
                if _index_file(conn, logical, abs_path):
                    updated += 1
                    if not quiet:
                        print(L(f"  проиндексирован {logical}", f"  indexed {logical}"), flush=True)
            if only is None:
                for row in conn.execute("SELECT path FROM file_state").fetchall():
                    if row["path"] not in current:
                        _remove_path(conn, row["path"])
                        removed += 1
                # фрагменты, у которых нет записи о файле (прерванная синхронизация)
                conn.execute("DELETE FROM chunks WHERE path NOT IN (SELECT path FROM file_state)")
                conn.execute("DELETE FROM embeddings WHERE path NOT IN (SELECT path FROM file_state)")
            meta_set(conn, "last_sync_at", str(time.time()))
            conn.commit()
            embedded = failed = 0
            complete = True
            if embedder.enabled and not text_only:
                deadline = started + max_seconds if max_seconds else None
                embedded, failed, complete = embed_missing(conn, embedder, targets, deadline, quiet)
            stats = index_counts(conn)
            stats.update(
                {
                    "files_seen": len(files),
                    "updated": updated,
                    "removed": removed,
                    "embedded_now": embedded,
                    "embedding_failures": failed,
                    "embeddings_complete": complete,
                    "embedding_error": None if complete else (embedder.last_error or meta_get(conn, "last_embedding_error")),
                    "seconds": round(time.time() - started, 2),
                }
            )
            return stats
        finally:
            conn.close()


def index_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    return {
        "files": conn.execute("SELECT count(*) FROM file_state").fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT count(*) FROM embeddings").fetchone()[0],
    }


def try_sync(**kwargs: Any) -> Dict[str, Any]:
    """Синхронизация, которая никогда не падает: для MCP и хуков."""
    try:
        return {"ok": True, **sync(**kwargs)}
    except SyncBusy:
        note = L("индекс уже обновляется фоновой задачей", "the index is already being updated by a background task")
        return {"ok": True, "busy": True, "note": note}
    except Exception as exc:
        return {"ok": False, "error": one_line(str(exc), 300)}


# ---------------------------------------------------------------- поиск

_token_re = re.compile(r"[\w@./:-]+", re.UNICODE)
_cyrillic_word = re.compile(r"^[а-яё]+$")
STOPWORDS = {
    "и", "в", "во", "на", "не", "что", "как", "мы", "я", "ты", "вы", "он", "она", "они", "это", "этот", "эта",
    "для", "или", "так", "там", "тут", "вот", "уже", "еще", "ещё", "при", "про", "где", "когда", "чтобы",
    "если", "ли", "же", "бы", "по", "от", "до", "из", "за", "со", "у", "о", "об", "а", "но", "да", "нет",
    "мне", "меня", "мой", "моя", "мои", "наш", "наши", "его", "ее", "её", "их", "был", "была", "было",
    "кто", "какой", "какая", "какое", "какие", "каких", "каким", "чем", "чём", "почему", "зачем", "сколько",
    "который", "которая", "которые", "можно", "нужно", "надо", "есть", "будет", "сейчас", "тогда", "всё",
    "все", "весь", "свой", "своя", "свои", "себя", "тебя", "тебе", "нам", "нас", "вас", "вам", "очень",
    "только", "тоже", "также", "даже", "ну", "вообще", "раньше", "прошлый", "прошлой", "помнишь", "знаешь",
    "the", "a", "an", "and", "or", "for", "with", "how", "what", "to", "of", "in", "on", "is", "are", "we",
}


def query_tokens(query: str) -> List[str]:
    tokens = [tok.strip(".:/-") for tok in _token_re.findall(query.lower())]
    tokens = [tok for tok in tokens if len(tok) >= 2]
    meaningful = [tok for tok in tokens if tok not in STOPWORDS]
    if meaningful:
        tokens = meaningful
    unique: List[str] = []
    for tok in tokens:
        if tok not in unique:
            unique.append(tok)
    return unique


_RU_ENDINGS = sorted(
    """иями ями ами иях ях ах ией ием иям ям ам ого его ому ему ыми ими ых их ую юю ая яя ое ее ые ие ый ий ой ей
    ом ем ов ев ия ии ью ться тся ешь ишь ете ите ают яют уют ют ят ут ат ет ит ла ло ли ть ы и а я о е у ю ь й""".split(),
    key=len,
    reverse=True,
)


def stem(token: str) -> str:
    """Лёгкая основа русского слова: «почту», «почты», «почтой» → «почт»; «Беты», «Бета» → «бет»."""
    if not _cyrillic_word.match(token) or len(token) < 4:
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 3:
            return token[: -len(ending)]
    return token


def fts_term(token: str) -> str:
    safe = token.replace('"', " ").strip()
    if _cyrillic_word.match(safe) and len(safe) >= 4:
        return f'"{stem(safe)}"*'
    return f'"{safe}"'


def _row_payload(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "chunk_key": row["chunk_key"],
        "path": row["path"],
        "abs_path": row["abs_path"],
        "heading": row["heading"] or "",
        "start_line": int(row["start_line"]),
        "end_line": int(row["end_line"]),
        "text": row["text"] or "",
    }


def _fts_query(conn: sqlite3.Connection, match: str, limit: int) -> List[sqlite3.Row]:
    try:
        return conn.execute(
            """SELECT chunk_key, path, abs_path, heading, start_line, end_line, text, bm25(chunks) AS rank
               FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?""",
            (match, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def lexical_candidates(conn: sqlite3.Connection, query: str, limit: int) -> Dict[str, Dict[str, Any]]:
    tokens = query_tokens(query)
    if not tokens:
        return {}
    terms = [fts_term(tok) for tok in tokens]
    results: Dict[str, Dict[str, Any]] = {}

    q_stems = [stem(tok) for tok in tokens]

    def add(rows: List[sqlite3.Row], factor: float, by_coverage: bool) -> None:
        # bm25 в SQLite отрицательный: чем меньше, тем лучше. Нормируем на лучший результат.
        best = max((abs(float(r["rank"] or 0.0)) for r in rows), default=0.0) or 1.0
        for row in rows:
            if row["chunk_key"] in results:
                continue
            payload = _row_payload(row)
            score = factor * abs(float(row["rank"] or 0.0)) / best
            if by_coverage:  # совпало одно слово из четырёх — это слабый сигнал
                doc = _stems_of(f"{row['heading']}\n{row['text']}")
                score *= sum(1 for s_ in q_stems if s_ in doc) / len(q_stems)
            payload["fts_score"] = score
            results[row["chunk_key"]] = payload

    add(_fts_query(conn, " AND ".join(terms), limit), 1.0, False)
    if len(results) < limit and len(terms) > 1:
        add(_fts_query(conn, " OR ".join(terms), limit), 0.8, True)
    if results:
        return results
    # запасной путь: подстрочный поиск по основам слов
    stems = [stem(tok) for tok in tokens]
    clauses, params = [], []  # type: List[str], List[Any]
    for item in stems:
        clauses.append("(lower(text) LIKE ? OR lower(heading) LIKE ? OR lower(path) LIKE ?)")
        params.extend([f"%{item}%"] * 3)
    rows = conn.execute(
        f"SELECT chunk_key, path, abs_path, heading, start_line, end_line, text FROM chunks WHERE {' OR '.join(clauses)}",
        params,
    ).fetchall()
    scored = []
    for row in rows:
        haystack = f"{row['path']} {row['heading']} {row['text']}".lower()
        hits = sum(1 for item in stems if item in haystack)
        scored.append((hits, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    for hits, row in scored[:limit]:
        payload = _row_payload(row)
        payload["fts_score"] = 0.3 * hits / max(1, len(stems))
        results[row["chunk_key"]] = payload
    return results


def vector_candidates(
    conn: sqlite3.Connection, embedder: Embedder, query: str, limit: int
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    if not embedder.enabled:
        return {}, None
    try:
        query_vec = normalize_vector(embedder.embed([query])[0])
    except Exception as exc:
        return {}, str(exc)
    rows = conn.execute(
        "SELECT chunk_key, vector FROM embeddings WHERE model = ? AND dims = ?", (embedder.model, len(query_vec))
    ).fetchall()
    if not rows:
        return {}, None
    scored: List[Tuple[float, str]]
    try:
        import numpy as np  # необязательно: просто быстрее на больших индексах

        matrix = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        sims = matrix @ np.asarray(query_vec, dtype=np.float32)
        order = np.argsort(-sims)[:limit]
        scored = [(float(sims[i]), rows[int(i)]["chunk_key"]) for i in order]
    except Exception:
        q = array.array("f", query_vec)
        scored = [(sum(map(operator.mul, q, blob_to_vector(r["vector"]))), r["chunk_key"]) for r in rows]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        scored = scored[:limit]
    by_key = {key: score for score, key in scored}
    results: Dict[str, Dict[str, Any]] = {}
    if by_key:
        marks = ",".join("?" for _ in by_key)
        for row in conn.execute(
            f"SELECT chunk_key, path, abs_path, heading, start_line, end_line, text FROM chunks WHERE chunk_key IN ({marks})",
            list(by_key),
        ):
            payload = _row_payload(row)
            payload["vector_score"] = by_key[row["chunk_key"]]
            results[row["chunk_key"]] = payload
    return results, None


# Вектор без совпадения слов учитывается с понижающим множителем GATE_FLOOR: так случайная
# «смысловая» близость не перебивает точные совпадения, но перефразировки всё равно находятся.
GATE_FLOOR = 0.45
FTS_SUPPORT_SCALE = 2.0
CURATED_BOOST = 1.1


def _stems_of(text: str) -> Set[str]:
    return {stem(tok.strip(".:/-")) for tok in _token_re.findall(text.lower()) if len(tok) >= 2}


def rerank(
    config: Dict[str, Any], query: str, lexical: Dict[str, Dict[str, Any]], vector: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for source in (lexical, vector):
        for key, payload in source.items():
            merged.setdefault(key, {}).update(payload)
    weights = config.get("rerank") or {}
    w_vec = float(weights.get("vectorWeight", 0.55))
    w_fts = float(weights.get("ftsWeight", 0.2))
    w_tok = float(weights.get("tokenWeight", 0.15))
    w_path = float(weights.get("pathWeight", 0.1))
    total = (w_vec + w_fts + w_tok + w_path) or 1.0
    q_stems = [stem(tok) for tok in query_tokens(query)]
    results = []
    for payload in merged.values():
        doc_stems = _stems_of(f"{payload.get('heading', '')}\n{payload.get('text', '')}")
        overlap = (sum(1 for s in q_stems if s in doc_stems) / len(q_stems)) if q_stems else 0.0
        target = f"{payload.get('path', '')} {payload.get('heading', '')}".lower()
        path_score = (sum(1 for s in q_stems if s in target) / len(q_stems)) if q_stems else 0.0
        vec = float(payload.get("vector_score", 0.0))
        fts = float(payload.get("fts_score", 0.0))
        support = max(overlap, path_score, min(1.0, fts * FTS_SUPPORT_SCALE))
        gated = max(0.0, vec) * (GATE_FLOOR + (1.0 - GATE_FLOOR) * support)
        payload["score"] = (w_vec * gated + w_fts * fts + w_tok * overlap + w_path * path_score) / total
        if payload.get("path") == "MEMORY.md":
            payload["score"] *= CURATED_BOOST  # отобранные факты надёжнее дневников
        payload["scores"] = {
            "vector": round(vec, 4),
            "gated_vector": round(gated, 4),
            "fts": round(fts, 4),
            "token": round(overlap, 4),
            "path": round(path_score, 4),
        }
        results.append(payload)
    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def best_snippet(text: str, query: str, max_lines: int = 6) -> str:
    lines = text.splitlines() or [text]
    stems = [stem(tok) for tok in query_tokens(query)]
    best_idx, best_score = 0, -1
    for idx, line in enumerate(lines):
        lower = line.lower()
        score = sum(lower.count(item) for item in stems)
        if score > best_score:
            best_idx, best_score = idx, score
    start = max(0, best_idx - 1)
    return "\n".join(lines[start:start + max_lines]).strip()


def search(query: str, limit: int = 8, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or load_config()
    limit = max(1, min(int(limit), 50))
    conn = connect()
    try:
        search_cfg = config.get("search") or {}
        lexical = lexical_candidates(conn, query, max(limit * 6, int(search_cfg.get("ftsCandidates", 64))))
        embedder = Embedder(config, timeout=25)  # запрос не должен висеть минутами
        vector, vector_error = vector_candidates(
            conn, embedder, query, max(limit * 6, int(search_cfg.get("vectorCandidates", 64)))
        )
        ranked = rerank(config, query, lexical, vector)[:limit]
        if ranked:  # слабые хвосты только тратят токены агента
            floor = max(0.05, float(ranked[0]["score"]) * 0.3)
            ranked = [ranked[0]] + [item for item in ranked[1:] if float(item["score"]) >= floor]
        results = []
        for item in ranked:
            results.append(
                {
                    "path": item["path"],
                    "heading": item.get("heading", ""),
                    "start_line": item["start_line"],
                    "end_line": item["end_line"],
                    "score": round(float(item["score"]), 4),
                    "scores": item["scores"],
                    "snippet": redact(best_snippet(str(item.get("text", "")), query)),
                }
            )
        counts = index_counts(conn)
        return {
            "query": query,
            "results": results,
            "semantic": bool(embedder.enabled and vector_error is None and counts["embeddings"] > 0),
            "semantic_error": vector_error,
            "index": counts,
        }
    finally:
        conn.close()


def format_search(payload: Dict[str, Any]) -> str:
    results = payload.get("results") or []
    lines: List[str] = []
    if payload.get("index", {}).get("files", 0) == 0:
        return L(
            "Индекс пуст: память ещё не проиндексирована (memory sync) или в ней нет заметок.",
            "The index is empty: the memory has not been indexed yet (memory sync) or it has no notes.",
        )
    if not results:
        lines.append(L(
            f"По запросу «{payload.get('query')}» ничего не найдено. Попробуй другие ключевые слова.",
            f"Nothing found for \"{payload.get('query')}\". Try other keywords.",
        ))
    for idx, item in enumerate(results, start=1):
        head = L(
            f"[{idx}] {item['path']}:{item['start_line']}-{item['end_line']}  (оценка {item['score']:.3f})",
            f"[{idx}] {item['path']}:{item['start_line']}-{item['end_line']}  (score {item['score']:.3f})",
        )
        lines.append(head)
        if item.get("heading"):
            lines.append(f"    § {item['heading']}")
        for line in item["snippet"].splitlines():
            lines.append(f"    {line}")
        lines.append("")
    if payload.get("semantic_error"):
        lines.append(L(
            f"(смысловой поиск недоступен — искал только по словам: {one_line(payload['semantic_error'], 160)})",
            f"(semantic search unavailable — searched by keywords only: {one_line(payload['semantic_error'], 160)})",
        ))
    if results:
        lines.append(L(
            "Полный текст фрагмента: memory_show(path, start, end).",
            "Full text of a fragment: memory_show(path, start, end).",
        ))
    return "\n".join(lines).rstrip()


def show(path: str, start: int = 1, end: Optional[int] = None) -> str:
    start = max(1, int(start))
    end = int(end) if end else start + 80
    end = min(end, start + 400)
    conn = connect()
    try:
        row = conn.execute("SELECT abs_path FROM file_state WHERE path = ?", (path,)).fetchone()
    finally:
        conn.close()
    if row:
        target = Path(row["abs_path"])
    else:
        target = (HOME_DIR / path).resolve()
        try:
            target.relative_to(HOME_DIR)
        except ValueError:
            raise ValueError(L(f"нет такого файла в памяти: {path}", f"no such file in the memory: {path}"))
    if not target.exists():
        raise ValueError(L(f"файл не найден: {path}", f"file not found: {path}"))
    lines = read_text(target).splitlines()
    end = min(len(lines), end)
    out = [f"{idx:>5}  {lines[idx - 1]}" for idx in range(start, end + 1)]
    footer = L(f"\n… (всего строк: {len(lines)})", f"\n… (total lines: {len(lines)})") if end < len(lines) else ""
    return redact("\n".join(out)) + footer


# ---------------------------------------------------------------- запись

def _normalize_note(text: str) -> str:
    text = re.sub(r"^\s*-\s*", "", text)
    text = re.sub(r"^(\d{1,2}:\d{2})\s*·\s*", "", text)
    text = re.sub(r"^\*\*[^*]{0,60}:\*\*\s*", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def daily_file(day: Optional[str] = None) -> Path:
    return NOTES_DIR / f"{day or today_str()}.md"


def remember(note: str, source: Optional[str] = None) -> Dict[str, Any]:
    cleaned = redact(note).strip()
    if not cleaned:
        raise ValueError(L("пустая заметка", "empty note"))
    target = daily_file()
    existing = read_text(target) if target.exists() else ""
    key = _normalize_note(cleaned.splitlines()[0])
    for line in existing.splitlines():
        if line.startswith("- ") and _normalize_note(line) == key:
            return {"saved": False, "duplicate": True, "path": str(target)}
    lines = cleaned.splitlines()
    entry = f"- {now_local().strftime('%H:%M')} · {lines[0].strip()}\n"
    for extra in lines[1:]:
        entry += f"  {extra.rstrip()}\n" if extra.strip() else ""
    if source and source.strip():
        entry += L(f"  — источник: {one_line(redact(source), 200)}\n", f"  — source: {one_line(redact(source), 200)}\n")
    header = "" if existing else L(f"# Дневник памяти — {today_str()}\n\n", f"# Memory journal — {today_str()}\n\n")
    append_text(target, header + entry)
    return {"saved": True, "path": str(target)}


def curate(note: str, category: str = "other", source: Optional[str] = None) -> Dict[str, Any]:
    cleaned = one_line(redact(note), 2000)
    if not cleaned:
        raise ValueError(L("пустая заметка", "empty note"))
    text = read_text(MEMORY_FILE) if MEMORY_FILE.exists() else L("# MEMORY.md — главная память\n", "# MEMORY.md — main memory\n")
    key = _normalize_note(cleaned)
    for line in text.splitlines():
        if line.startswith("- ") and _normalize_note(line) == key:
            return {"saved": False, "duplicate": True, "path": str(MEMORY_FILE), "chars": len(text)}
    if cleaned.lower() in text.lower():
        return {"saved": False, "duplicate": True, "path": str(MEMORY_FILE), "chars": len(text)}
    label = CATEGORY_LABELS.get((category or "other").strip().lower(), (category or L("заметка", "note")).strip().lower())
    src = f" _({one_line(redact(source), 120)})_" if source and source.strip() else ""
    entry = f"- **{today_str()} · {label}:** {cleaned}{src}"
    lines = text.rstrip("\n").splitlines()
    try:
        idx = next(i for i, line in enumerate(lines) if line.strip() in CURATED_SECTIONS)
        insert_at = idx + 1
        if insert_at < len(lines) and lines[insert_at].strip().startswith("<!--"):
            while insert_at < len(lines) and "-->" not in lines[insert_at]:
                insert_at += 1  # пропускаем служебный комментарий под заголовком
            insert_at += 1
        lines.insert(min(insert_at, len(lines)), entry)
    except StopIteration:
        lines += ["", curated_section(), entry]
    new_text = "\n".join(lines) + "\n"
    atomic_write(MEMORY_FILE, new_text)
    result: Dict[str, Any] = {"saved": True, "path": str(MEMORY_FILE), "chars": len(new_text)}
    if len(new_text) > MEMORY_CONTEXT_LIMIT:
        result["warning"] = L(
            f"MEMORY.md уже {len(new_text)} символов (в начало сессии попадает {MEMORY_CONTEXT_LIMIT}). "
            "Сожми: объедини похожие пункты, устаревшее убери через memory_forget.",
            f"MEMORY.md is already {len(new_text)} characters (the start of a session gets {MEMORY_CONTEXT_LIMIT}). "
            "Shorten it: merge similar items and remove outdated ones with memory_forget.",
        )
    return result


def _bullet_blocks(lines: List[str]) -> List[Tuple[int, int]]:
    """Границы пунктов «- …» вместе со строками-продолжениями (с отступом)."""
    blocks = []
    idx = 0
    while idx < len(lines):
        if lines[idx].startswith("- "):
            end = idx + 1
            while end < len(lines) and lines[end].startswith("  ") and lines[end].strip():
                end += 1
            blocks.append((idx, end))
            idx = end
        else:
            idx += 1
    return blocks


def forget(text: str, scope: str = "curated", all_matches: bool = False) -> Dict[str, Any]:
    needle = (text or "").strip().lower()
    if len(needle) < 4:
        raise ValueError(L(
            "укажи фрагмент текста подлиннее (от 4 символов), чтобы не удалить лишнее",
            "give a longer text fragment (4 characters or more), so that nothing extra is deleted",
        ))
    files = [MEMORY_FILE] if MEMORY_FILE.exists() else []
    if scope == "all" and NOTES_DIR.exists():
        files += sorted(NOTES_DIR.rglob("*.md"))
    matches = []
    for path in files:
        lines = read_text(path).splitlines()
        for start, end in _bullet_blocks(lines):
            block = "\n".join(lines[start:end])
            if needle in block.lower():
                matches.append((path, start, end, block))
    if not matches:
        return {"removed": 0, "message": L("ничего не найдено", "nothing found")}
    if len(matches) > 1 and not all_matches:
        return {
            "removed": 0,
            "message": L(
                f"совпадений: {len(matches)} — уточни текст или передай all_matches=true",
                f"matches: {len(matches)} — make the text more specific or pass all_matches=true",
            ),
            "candidates": [f"{rel_to_home(p)}: {one_line(b, 160)}" for p, _, _, b in matches[:8]],
        }
    trash = STATE_DIR / "forgotten.md"
    by_file: Dict[Path, List[Tuple[int, int, str]]] = {}
    for path, start, end, block in matches:
        by_file.setdefault(path, []).append((start, end, block))
    for path, blocks in by_file.items():
        lines = read_text(path).splitlines()
        for start, end, block in sorted(blocks, reverse=True):
            del lines[start:end]
            stamp, origin = f"{now_local():%Y-%m-%d %H:%M}", rel_to_home(path)
            append_text(trash, L(f"- {stamp} из {origin}:\n", f"- {stamp} from {origin}:\n") + f"  {block.replace(chr(10), chr(10) + '  ')}\n")
        atomic_write(path, "\n".join(lines).rstrip("\n") + "\n")
    try_sync(only=list(by_file), max_seconds=60)
    return {"removed": len(matches), "files": [rel_to_home(p) for p in by_file], "trash": rel_to_home(trash)}


# ---------------------------------------------------------------- контекст сессии

def _strip_comments(text: str) -> str:
    return re.sub(r"<!--[\s\S]*?-->", "", text)


def recent_notes(days: int = 7, limit: int = 12) -> List[str]:
    if not NOTES_DIR.exists():
        return []
    files = sorted(NOTES_DIR.glob("????-??-??.md"), reverse=True)
    cutoff = (now_local() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    collected: List[str] = []
    for path in files:
        day = path.stem
        if day < cutoff:
            break
        lines = read_text(path).splitlines()
        entries = [lines[s] for s, _ in _bullet_blocks(lines)]
        for line in reversed(entries):
            collected.append(f"- {day} {one_line(line[2:], 220)}")
            if len(collected) >= limit:
                return collected
    return collected


def build_session_context(limit: int = MEMORY_CONTEXT_LIMIT) -> str:
    parts = [
        "<extended_memory>",
        L(
            "Это расширенная память пользователя между сессиями (MCP-сервер extended-memory).",
            "This is the user's extended memory between sessions (the extended-memory MCP server).",
        ),
        L(
            "Ниже главная память (MEMORY.md) и последние записи дневника. Остальное — дневники, история прошлых "
            "сессий, подключённые папки — ищи через memory_search, подробности открывай через memory_show.",
            "Below are the main memory (MEMORY.md) and the recent journal entries. For everything else — journals, the "
            "history of past sessions, connected folders — use memory_search, and open the details with memory_show.",
        ),
        L(
            "Долговечное сохраняй: memory_remember (дневник), memory_curate (главная память, коротко и без дублей). "
            "Секреты не сохраняй никогда.",
            "Save lasting knowledge: memory_remember (journal), memory_curate (main memory, short and without duplicates). "
            "Never save secrets.",
        ),
        "",
    ]
    if MEMORY_FILE.exists():
        body = _strip_comments(read_text(MEMORY_FILE))
        body = "\n".join(line for line in body.splitlines() if line.strip() not in ("-", "- ")).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        if len(body) > limit:
            body = body[:limit].rsplit("\n", 1)[0] + L(
                "\n… (MEMORY.md обрезан — остальное найдётся через memory_search)",
                "\n… (MEMORY.md is cut off — the rest can be found with memory_search)",
            )
        parts += ["=== MEMORY.md ===", redact(body), ""]
    notes = recent_notes()
    if notes:
        parts += [L("=== Последние записи дневника ===", "=== Recent journal entries ==="), *[redact(n) for n in notes], ""]
    parts.append("</extended_memory>")
    return "\n".join(parts)


# ---------------------------------------------------------------- обслуживание

def maintain(quiet: bool = True) -> Dict[str, Any]:
    """Чистит секреты, убирает дубли пунктов в дневниках, обновляет индекс."""
    changed_files = removed_dupes = 0
    files = sorted(NOTES_DIR.rglob("*.md")) if NOTES_DIR.exists() else []
    for path in files:
        raw = read_text(path)
        lines = redact(raw).splitlines()
        blocks = _bullet_blocks(lines)
        seen: Set[str] = set()
        drop: List[Tuple[int, int]] = []
        for start, end in blocks:
            key = _normalize_note(" ".join(lines[start:end]))
            if key in seen:
                drop.append((start, end))
            seen.add(key)
        for start, end in reversed(drop):
            del lines[start:end]
        new_text = "\n".join(lines).rstrip("\n") + "\n"
        if new_text != raw:
            atomic_write(path, new_text)
            changed_files += 1
            removed_dupes += len(drop)
    if MEMORY_FILE.exists():
        raw = read_text(MEMORY_FILE)
        clean = redact(raw)
        if clean != raw:
            atomic_write(MEMORY_FILE, clean)
            changed_files += 1
    result: Dict[str, Any] = {"changed_files": changed_files, "removed_duplicates": removed_dupes}
    result["sync"] = try_sync(quiet=quiet)
    return result


# ---------------------------------------------------------------- история Codex

TRIVIAL_RE = re.compile(r"^(ок|ok|окей|да|нет|спасибо|спс|thanks|thx|понял|ага|угу|продолжай|дальше|go|continue|yes|no)[.!]*$", re.I)
SIGNAL_RE = re.compile(
    r"\b(сделал\w*|сделан\w*|готов\w*|добавил\w*|добавлен\w*|настроил\w*|настроен\w*|исправил\w*|исправлен\w*|"
    r"починил\w*|решил\w*|решени\w*|подключил\w*|подключен\w*|подключён\w*|создал\w*|создан\w*|обновил\w*|"
    r"обновлен\w*|обновлён\w*|удалил\w*|перенес\w*|перенёс\w*|запустил\w*|развернул\w*|внедрил\w*|важн\w*|"
    r"итог\w*|выяснил\w*|причин\w*|fixed|added|created|updated|configured|deployed|implemented|migrated|"
    r"refactor\w*|decided|decision|root cause|done)\b",
    re.I,
)
_INJECTED_PREFIXES = ("<", "# AGENTS.md instructions", "# Context from my IDE")


def extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("input_text", "text")]
    else:
        return ""
    kept = []
    for part in parts:
        text = str(part or "")
        marker = "## My request for Codex:"
        if marker in text:
            text = text.split(marker, 1)[1]
        stripped = text.strip()
        if not stripped or stripped.startswith(_INJECTED_PREFIXES):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def is_significant(user: str, assistant: str, need_signal: bool = True) -> bool:
    user, assistant = (user or "").strip(), (assistant or "").strip()
    if not assistant or len(f"{user}\n{assistant}") < 200:
        return False
    if user and TRIVIAL_RE.match(user):
        return False
    return bool(SIGNAL_RE.search(f"{user}\n{assistant}")) if need_signal else True


def format_turn(when: dt.datetime, cwd: str, user: str, assistant: str) -> str:
    project = Path(cwd).name if cwd else ""
    where = f" · `{project}`" if project else ""
    user_line = one_line(redact(user), 500) or L("(запрос не найден)", "(request not found)")
    result_line = one_line(redact(assistant), 900)
    return L(
        f"- {when:%H:%M}{where} · запрос: {user_line}\n  итог: {result_line}\n",
        f"- {when:%H:%M}{where} · request: {user_line}\n  result: {result_line}\n",
    )


def append_turn(folder: Path, when: dt.datetime, cwd: str, user: str, assistant: str, kind: str) -> Path:
    target = folder / f"{when:%Y-%m-%d}.md"
    header = "" if target.exists() else f"# {kind} — {when:%Y-%m-%d}\n\n"
    append_text(target, header + format_turn(when, cwd, user, assistant))
    return target


def _load_id_state(name: str) -> Dict[str, Any]:
    path = STATE_DIR / name
    try:
        return json.loads(read_text(path))
    except Exception:
        return {}


def _save_id_state(name: str, state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(STATE_DIR / name, json.dumps(state, ensure_ascii=False))


def _to_datetime(value: Any, fallback: dt.datetime) -> dt.datetime:
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            number = float(value)
            if number > 1e12:
                number /= 1000.0
            return dt.datetime.fromtimestamp(number)
        if isinstance(value, str) and value:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, OverflowError, OSError):
        pass
    return fallback


def iter_rollout_turns(path: Path) -> Iterator[Dict[str, Any]]:
    """Ходы из файла сессии Codex (rollout-*.jsonl): запрос пользователя + итоговый ответ."""
    cwd = ""
    user = ""
    fallback = dt.datetime.fromtimestamp(path.stat().st_mtime)
    needles = ('"session_meta"', '"turn_context"', '"user_message"', '"role":"user"', '"role": "user"', '"task_complete"')
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not any(n in line for n in needles):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("type")
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if kind in ("session_meta", "turn_context") and payload.get("cwd"):
                cwd = str(payload["cwd"])
            elif kind == "event_msg" and ptype == "user_message" and payload.get("message"):
                text = extract_user_text(str(payload["message"]))
                if text:
                    user = text
            elif kind == "response_item" and ptype == "message" and payload.get("role") == "user":
                text = extract_user_text(payload.get("content"))
                if text:
                    user = text
            elif kind == "event_msg" and ptype == "task_complete":
                when = _to_datetime(payload.get("completed_at") or row.get("timestamp"), fallback)
                yield {
                    "turn_id": str(payload.get("turn_id") or sha1_text(f"{path}:{when}:{user}")),
                    "when": when,
                    "cwd": cwd,
                    "user": user,
                    "assistant": str(payload.get("last_agent_message") or ""),
                }
                user = ""


def import_codex_history(days: int = 120, codex_home: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    home = Path(os.path.expanduser(codex_home or os.environ.get("CODEX_HOME") or "~/.codex"))
    roots = [home / "sessions", home / "archived_sessions"]
    cutoff = time.time() - days * 86400
    files = []
    for root in roots:
        if root.exists():
            files += [p for p in root.rglob("rollout-*.jsonl") if p.stat().st_mtime >= cutoff]
    state = _load_id_state("imported-turns.json")
    known: Dict[str, int] = state.get("turns", {})
    repeats: Dict[str, int] = {}
    imported = skipped = 0
    touched: Set[Path] = set()
    for path in sorted(files):
        try:
            turns = list(iter_rollout_turns(path))
        except OSError:
            continue
        for turn in turns:
            if turn["turn_id"] in known:
                continue
            if turn["when"].timestamp() < cutoff:
                continue
            key = one_line(turn["user"], 300).lower()
            repeats[key] = repeats.get(key, 0) + 1
            if not dry_run:
                known[turn["turn_id"]] = int(time.time())  # просмотренный ход больше не рассматриваем
            if repeats[key] > 3 or not is_significant(turn["user"], turn["assistant"], need_signal=False):
                skipped += 1
                continue
            if not dry_run:
                kind = L("История Codex", "Codex history")
                touched.add(append_turn(HISTORY_DIR, turn["when"], turn["cwd"], turn["user"], turn["assistant"], kind))
            imported += 1
    if not dry_run:
        _save_id_state("imported-turns.json", {"turns": known})
        for path in touched:  # внутри дня — по времени
            _sort_day_file(path)
    return {"sessions": len(files), "imported": imported, "skipped": skipped, "files": len(touched)}


def _sort_day_file(path: Path) -> None:
    lines = read_text(path).splitlines()
    blocks = _bullet_blocks(lines)
    if not blocks:
        return
    head = lines[: blocks[0][0]]
    items = ["\n".join(lines[s:e]) for s, e in blocks]
    items.sort(key=lambda block: block[2:7])
    atomic_write(path, "\n".join(head).rstrip("\n") + "\n\n" + "\n".join(items) + "\n")


# ---------------------------------------------------------------- папки и настройки

def add_folder(folder: str, prefix: Optional[str] = None, exts: Optional[str] = None) -> Dict[str, Any]:
    path = Path(os.path.expanduser(folder)).resolve()
    if not path.exists():
        raise ValueError(L(f"нет такой папки или файла: {path}", f"no such folder or file: {path}"))
    config = load_config()
    stored = rel_to_home(path)
    for root in config.get("roots", []):
        if expand_path(root.get("path", "")) == path:
            return {"added": False, "message": L("уже подключена", "already added"), "path": stored}
    taken = {str(r.get("logicalPrefix", "")) for r in config.get("roots", [])}
    base = (prefix or f"notes/{path.name}").strip("/")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    root: Dict[str, Any] = {"path": stored, "logicalPrefix": name}
    if exts:
        root["includeExts"] = [e if e.startswith(".") else f".{e}" for e in exts.split(",") if e.strip()]
    config.setdefault("roots", []).append(root)
    save_config(config)
    return {"added": True, "path": stored, "prefix": name}


def remove_folder(folder: str) -> Dict[str, Any]:
    path = Path(os.path.expanduser(folder)).resolve()
    config = load_config()
    before = len(config.get("roots", []))
    config["roots"] = [r for r in config.get("roots", []) if expand_path(r.get("path", "")) != path]
    if len(config["roots"]) == before:
        return {"removed": False, "message": L("такой папки нет в списке", "this folder is not in the list")}
    save_config(config)
    return {"removed": True}


def ollama_models(endpoint: str) -> Optional[List[str]]:
    parsed = urllib.parse.urlparse(endpoint)
    tags = f"{parsed.scheme}://{parsed.netloc}/api/tags"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(tags, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return None


def set_embeddings(state: str, model: Optional[str] = None, endpoint: Optional[str] = None) -> Dict[str, Any]:
    config = load_config()
    emb = config.setdefault("embedding", {})
    emb["enabled"] = state == "on"
    if model:
        emb["model"] = model
    if endpoint:
        emb["endpoint"] = endpoint
    save_config(config)
    result: Dict[str, Any] = {"enabled": emb["enabled"], "model": emb.get("model"), "endpoint": emb.get("endpoint")}
    if emb["enabled"]:
        probe = Embedder(config)
        try:
            probe.embed([L("проверка", "check")])
            result["check"] = "ok"
        except Exception as exc:
            result["check"] = str(exc)
    return result


# ---------------------------------------------------------------- статус и диагностика

def status() -> Dict[str, Any]:
    config = load_config()
    conn = connect()
    try:
        counts = index_counts(conn)
        last_sync = meta_get(conn, "last_sync_at")
        last_error = meta_get(conn, "last_embedding_error")
    finally:
        conn.close()
    emb = config.get("embedding") or {}
    return {
        "version": VERSION,
        "home": str(HOME_DIR),
        **counts,
        "db_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "last_sync": dt.datetime.fromtimestamp(float(last_sync)).strftime("%Y-%m-%d %H:%M") if last_sync else None,
        "embedding_enabled": bool(emb.get("enabled")),
        "embedding_model": emb.get("model"),
        "last_embedding_error": last_error,
        "memory_md_chars": len(read_text(MEMORY_FILE)) if MEMORY_FILE.exists() else 0,
        "roots": [
            {"path": rel_to_home(spec.abs_path), "prefix": spec.logical_prefix, "exists": spec.abs_path.exists()}
            for spec in build_root_specs(config)
        ],
    }


def format_status(info: Dict[str, Any]) -> str:
    size_mb = info["db_bytes"] / 1024 / 1024
    semantic = (
        L(f"включён ({info['embedding_model']})", f"on ({info['embedding_model']})")
        if info["embedding_enabled"]
        else L("выключен — поиск только по словам", "off — keyword search only")
    )
    last_sync = info["last_sync"] or L("ещё не было", "not yet")
    lines = [
        L(
            f"Расширенная память v{info['version']} — {rel_to_home(Path(info['home']))}",
            f"Extended memory v{info['version']} — {rel_to_home(Path(info['home']))}",
        ),
        L(
            f"Файлов в индексе: {info['files']}, фрагментов: {info['chunks']}, с векторами: {info['embeddings']} ({size_mb:.1f} МБ)",
            f"Files in the index: {info['files']}, fragments: {info['chunks']}, with vectors: {info['embeddings']} ({size_mb:.1f} MB)",
        ),
        L(f"Последняя синхронизация: {last_sync}", f"Last sync: {last_sync}"),
        L(f"Смысловой поиск: {semantic}", f"Semantic search: {semantic}"),
        L(
            f"MEMORY.md: {info['memory_md_chars']} символов (в сессию попадает до {MEMORY_CONTEXT_LIMIT})",
            f"MEMORY.md: {info['memory_md_chars']} characters (a session gets up to {MEMORY_CONTEXT_LIMIT})",
        ),
    ]
    if info["embedding_enabled"] and info["embeddings"] < info["chunks"]:
        lines.append(L(
            f"Векторы досчитываются: {info['embeddings']}/{info['chunks']}",
            f"Vectors are still being computed: {info['embeddings']}/{info['chunks']}",
        ))
    if info.get("last_embedding_error"):
        lines.append(L(
            f"Последняя ошибка эмбеддингов: {one_line(info['last_embedding_error'], 200)}",
            f"Last embedding error: {one_line(info['last_embedding_error'], 200)}",
        ))
    lines.append(L("Что индексируется:", "What is indexed:"))
    for root in info["roots"]:
        mark = "" if root["exists"] else L("  (нет на диске)", "  (not on disk)")
        lines.append(f"  - {root['path']} → {root['prefix']}{mark}")
    return "\n".join(lines)


def _codex_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))


def _language() -> Tuple[str, str]:
    """Название текущего языка и код второго — для подсказки «сменить: memory lang …»."""
    lang = i18n.current()
    return i18n.NAMES[lang], ("ru" if lang == "en" else "en")


def doctor() -> Tuple[List[Tuple[bool, str]], bool]:
    checks: List[Tuple[bool, str]] = []
    ok_py = sys.version_info >= (3, 8)
    checks.append((ok_py, f"Python {sys.version.split()[0]} ({sys.executable})"))
    try:
        mem = sqlite3.connect(":memory:")
        mem.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        checks.append((True, L(f"SQLite {sqlite3.sqlite_version} с FTS5", f"SQLite {sqlite3.sqlite_version} with FTS5")))
    except sqlite3.Error as exc:
        checks.append((False, L(
            f"в SQLite нет FTS5: {exc} — поставьте Python из Homebrew или python.org",
            f"SQLite has no FTS5: {exc} — install Python from Homebrew or python.org",
        )))
    checks.append((MEMORY_FILE.exists(), L(f"главная память {rel_to_home(MEMORY_FILE)}", f"main memory {rel_to_home(MEMORY_FILE)}")))
    name, other = _language()
    checks.append((True, L(f"язык: {name} (сменить: memory lang {other})", f"language: {name} (switch: memory lang {other})")))
    config = load_config()
    emb = config.get("embedding") or {}
    if emb.get("enabled"):
        models = ollama_models(str(emb.get("endpoint", DEFAULT_ENDPOINT)))
        if "/api/" in str(emb.get("endpoint", "")) and models is None:
            checks.append((False, L(
                "Ollama не отвечает на 127.0.0.1:11434 — запустите Ollama (иначе поиск только по словам)",
                "Ollama is not responding at 127.0.0.1:11434 — start Ollama (otherwise keyword search only)",
            )))
        else:
            probe = Embedder(config)
            try:
                probe.embed([L("проверка связи", "connection check")])
                checks.append((True, L(f"эмбеддинги работают ({probe.model})", f"embeddings work ({probe.model})")))
            except Exception as exc:
                checks.append((False, L(f"эмбеддинги: {one_line(str(exc), 200)}", f"embeddings: {one_line(str(exc), 200)}")))
    else:
        checks.append((True, L(
            "смысловой поиск выключен (включить: memory embeddings on)",
            "semantic search is off (turn it on: memory embeddings on)",
        )))
    info = status()
    checks.append((info["files"] > 0, L(
        f"индекс: файлов {info['files']}, фрагментов {info['chunks']}, векторов {info['embeddings']}",
        f"index: files {info['files']}, fragments {info['chunks']}, vectors {info['embeddings']}",
    )))
    codex = _codex_home()
    config_toml = codex / "config.toml"
    toml_text = read_text(config_toml) if config_toml.exists() else ""
    checks.append((f"[mcp_servers.{SERVER_NAME}]" in toml_text, L(
        f"MCP-сервер прописан в {rel_to_home(config_toml)}",
        f"MCP server is registered in {rel_to_home(config_toml)}",
    )))
    agents = codex / "AGENTS.md"
    checks.append((agents.exists() and "extended-memory:start" in read_text(agents), L(
        f"инструкции в {rel_to_home(agents)}",
        f"instructions in {rel_to_home(agents)}",
    )))
    hooks = codex / "hooks.json"
    hooks_text = read_text(hooks) if hooks.exists() else ""
    if "hook_session_start.py" in hooks_text or "hook_stop.py" in hooks_text:
        runs = _load_id_state("hook-runs.json")
        titles = (
            ("SessionStart", L("хук начала сессии", "session start hook")),
            ("Stop", L("хук автосохранения", "auto-record hook")),
        )
        for event, title in titles:
            if runs.get(event):
                checks.append((True, L(f"{title}: последний запуск {runs[event]}", f"{title}: last run {runs[event]}")))
            else:
                checks.append((False, L(
                    f"{title} ещё не запускался — откройте Кодекс и подтвердите доверие хукам (Trust)",
                    f"{title} has not run yet — open Codex and trust the hooks (Trust)",
                )))
    else:
        checks.append((True, L(
            "хуки не установлены (ставились с --no-hooks)",
            "hooks are not installed (the package was installed with --no-hooks)",
        )))
    return checks, all(ok for ok, _ in checks)


def record_hook_run(event: str) -> None:
    try:
        runs = _load_id_state("hook-runs.json")
        runs[event] = now_local().strftime("%Y-%m-%d %H:%M")
        _save_id_state("hook-runs.json", runs)
    except Exception:
        pass


def spawn_background_sync(paths: Sequence[Path]) -> None:
    """Запускает синхронизацию отдельным процессом и сразу возвращается (для хуков)."""
    args = [sys.executable, str(APP_DIR / "hybrid_memory.py"), "sync", "--quiet"]
    for path in paths:
        args += ["--only", str(path)]
    env = dict(os.environ, CODEX_MEMORY_HOME=str(HOME_DIR))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    detach: Dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":  # без окна консоли и отдельной группой, чтобы пережить хук
        detach = {"creationflags": 0x08000000 | 0x00000200}  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    with open(LOG_DIR / "background-sync.log", "a", encoding="utf-8") as log:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, close_fds=True, **detach)


# ---------------------------------------------------------------- CLI

def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _sync_phrase(result: Dict[str, Any]) -> str:
    if result.get("busy"):
        return L("индекс обновляется фоновой задачей", "the index is being updated by a background task")
    if not result.get("ok"):
        return L(f"индекс обновится позже: {result.get('error')}", f"the index will be updated later: {result.get('error')}")
    if result.get("embedding_error"):
        return L("проиндексировано по словам, векторы догонят позже", "indexed by keywords, the vectors will catch up later")
    return L("проиндексировано", "indexed")


def set_language(value: str) -> int:
    """Команда lang: сохраняет язык и переписывает на нём инструкции для агента в настройках Кодекса."""
    code = i18n.normalize(value)
    if code is None:
        raise ValueError(L(f"неизвестный язык: {value} (есть ru и en)", f"unknown language: {value} (use ru or en)"))
    i18n.save(code, HOME_DIR)
    i18n.use(code)  # дальше сообщения идут уже на новом языке
    script = APP_DIR / "configure_codex.py"
    updated = False
    if script.is_file():
        command = [sys.executable, str(script), "relang", "--lang", code,
                   "--mem-home", str(HOME_DIR), "--codex-home", str(_codex_home())]
        print(end="", flush=True)  # подпроцесс печатает свои строки сам: наши уже напечатанные должны встать раньше
        try:
            updated = subprocess.run(command, stdin=subprocess.DEVNULL).returncode == 0
        except OSError:
            updated = False
    if not updated:
        warn(L(
            "предупреждение: язык сохранён, но инструкции для агента в AGENTS.md Кодекса не обновлены — "
            "запустите установщик ещё раз",
            "warning: the language is saved, but the agent instructions in the Codex AGENTS.md were not updated — "
            "run the installer again",
        ))
    print(L(
        f"Язык памяти: {i18n.NAMES[code]}. Перезапустите Кодекс, чтобы сессии получили инструкции на новом языке.",
        f"Memory language: {i18n.NAMES[code]}. Restart Codex so that sessions pick up the instructions in the new language.",
    ))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    fix_stdio()
    parser = argparse.ArgumentParser(prog="memory", description=L("Расширенная память для Codex", "Extended memory for Codex"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="cmd", metavar=L("команда", "command"))
    sub.required = True

    def cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--json", action="store_true", help=L("вывод в JSON", "output as JSON"))
        return p

    cmd("status", L("состояние индекса", "index status"))
    p = cmd("sync", L("обновить индекс", "update the index"))
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--text-only", action="store_true", help=L("без векторов (быстро)", "without vectors (fast)"))
    p.add_argument("--only", action="append", help=L("обновить только этот файл (можно несколько)", "update only this file (can be repeated)"))
    p.add_argument("--max-seconds", type=float)
    p = cmd("search", L("поиск по памяти", "search the memory"))
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=8)
    p = cmd("show", L("показать строки файла из памяти", "show lines of a file from the memory"))
    p.add_argument("path")
    p.add_argument("start", type=int, nargs="?", default=1)
    p.add_argument("end", type=int, nargs="?")
    p = cmd("remember", L("записать факт в дневник", "write a fact to the journal"))
    p.add_argument("note", nargs="+")
    p.add_argument("--source")
    p = cmd("curate", L("записать факт в главную память MEMORY.md", "write a fact to the main memory MEMORY.md"))
    p.add_argument("note", nargs="+")
    p.add_argument("--category", default="other", choices=sorted(CATEGORY_LABELS))
    p.add_argument("--source")
    p = cmd("forget", L("удалить пункт памяти по фрагменту текста", "delete a memory item by a fragment of its text"))
    p.add_argument("text", nargs="+")
    p.add_argument("--all-files", action="store_true", help=L("искать и в дневниках, не только в MEMORY.md", "search the journals too, not only MEMORY.md"))
    p.add_argument("--all-matches", action="store_true", help=L("удалить все совпадения", "delete all matches"))
    cmd("maintain", L("чистка секретов и дублей + синхронизация", "scrub secrets and duplicates + sync"))
    p = cmd("import-codex-history", L("загрузить в память прошлые сессии Кодекса", "load past Codex sessions into the memory"))
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--codex-home")
    p.add_argument("--dry-run", action="store_true")
    p = cmd("add-folder", L("подключить папку с заметками к поиску", "add a notes folder to search"))
    p.add_argument("folder")
    p.add_argument("--prefix")
    p.add_argument("--ext", help=L("расширения через запятую, по умолчанию .md,.txt", "comma-separated extensions, .md,.txt by default"))
    p = cmd("remove-folder", L("отключить папку от поиска", "remove a folder from search"))
    p.add_argument("folder")
    p = cmd("embeddings", L("включить/выключить смысловой поиск", "turn semantic search on or off"))
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--model")
    p.add_argument("--endpoint")
    p = cmd("context", L("показать, что подмешивается в начало сессии", "show what is added to the start of a session"))
    p.add_argument("--max-chars", type=int, default=MEMORY_CONTEXT_LIMIT)
    cmd("doctor", L("проверить установку", "check the installation"))
    lang_help = L("показать или сменить язык памяти: ru или en", "show or switch the memory language: ru or en")
    p = sub.add_parser("lang", help=lang_help, description=lang_help)
    p.add_argument("code", nargs="?", metavar="ru|en", help=L("новый язык; без него — показать текущий", "the new language; without it, show the current one"))

    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        if args.cmd == "status":
            info = status()
            _print_json(info) if as_json else print(format_status(info))
        elif args.cmd == "sync":
            only = [Path(p) for p in args.only] if args.only else None
            try:
                result = sync(only=only, text_only=args.text_only, max_seconds=args.max_seconds, quiet=args.quiet or as_json)
            except SyncBusy:
                if as_json:
                    _print_json({"busy": True})
                elif not args.quiet:
                    print(L(
                        "Индекс уже обновляется другим процессом — пропускаю.",
                        "The index is already being updated by another process — skipping.",
                    ))
                return 0
            if as_json:
                _print_json(result)
            elif not args.quiet:
                print(L(
                    f"Готово за {result['seconds']} с: файлов {result['files']}, обновлено {result['updated']}, "
                    f"удалено {result['removed']}, фрагментов {result['chunks']}, векторов {result['embeddings']}.",
                    f"Done in {result['seconds']} s: files {result['files']}, updated {result['updated']}, "
                    f"removed {result['removed']}, fragments {result['chunks']}, vectors {result['embeddings']}.",
                ))
            if result.get("embedding_error") and not text_only_requested(args):
                warn(L(f"эмбеддинги: {result['embedding_error']}", f"embeddings: {result['embedding_error']}"))
        elif args.cmd == "search":
            payload = search(" ".join(args.query), args.limit)
            _print_json(payload) if as_json else print(format_search(payload))
        elif args.cmd == "show":
            print(show(args.path, args.start, args.end))
        elif args.cmd == "remember":
            result = remember(" ".join(args.note), args.source)
            if result.get("saved"):
                result["sync"] = try_sync(only=[Path(result["path"])], max_seconds=60)
            if as_json:
                _print_json(result)
            elif result.get("saved"):
                print(L(
                    f"Сохранено в {rel_to_home(Path(result['path']))} ({_sync_phrase(result['sync'])}).",
                    f"Saved to {rel_to_home(Path(result['path']))} ({_sync_phrase(result['sync'])}).",
                ))
            else:
                print(L("Такой пункт в сегодняшнем дневнике уже есть.", "Today's journal already has this item."))
        elif args.cmd == "curate":
            result = curate(" ".join(args.note), args.category, args.source)
            if result.get("saved"):
                result["sync"] = try_sync(only=[MEMORY_FILE], max_seconds=60)
            if as_json:
                _print_json(result)
            elif result.get("saved"):
                print(L(
                    f"Добавлено в {rel_to_home(MEMORY_FILE)} ({_sync_phrase(result['sync'])}).",
                    f"Added to {rel_to_home(MEMORY_FILE)} ({_sync_phrase(result['sync'])}).",
                ))
                if result.get("warning"):
                    print(L(f"Внимание: {result['warning']}", f"Note: {result['warning']}"))
            else:
                print(L("Такой факт в MEMORY.md уже есть.", "MEMORY.md already has this fact."))
        elif args.cmd == "forget":
            result = forget(" ".join(args.text), "all" if args.all_files else "curated", args.all_matches)
            if as_json:
                _print_json(result)
            elif result.get("removed"):
                print(L(
                    f"Удалено пунктов: {result['removed']} ({', '.join(result['files'])}). Копия — в {result['trash']}.",
                    f"Items deleted: {result['removed']} ({', '.join(result['files'])}). A copy is in {result['trash']}.",
                ))
            else:
                print(L(f"Ничего не удалено: {result.get('message')}.", f"Nothing deleted: {result.get('message')}."))
                for candidate in result.get("candidates", []):
                    print(f"  - {candidate}")
        elif args.cmd == "maintain":
            result = maintain()
            if as_json:
                _print_json(result)
            else:
                print(L(
                    f"Почищено файлов: {result['changed_files']}, убрано дублей: {result['removed_duplicates']}; "
                    f"{_sync_phrase(result['sync'])}.",
                    f"Files cleaned: {result['changed_files']}, duplicates removed: {result['removed_duplicates']}; "
                    f"{_sync_phrase(result['sync'])}.",
                ))
        elif args.cmd == "import-codex-history":
            result = import_codex_history(args.days, args.codex_home, args.dry_run)
            if as_json:
                _print_json(result)
            else:
                verb = L("нашлось бы", "that would be loaded") if args.dry_run else L("загружено", "loaded")
                print(L(
                    f"Сессий за {args.days} дн.: {result['sessions']}; {verb} значимых ходов: {result['imported']} "
                    f"(пропущено мелких и повторов: {result['skipped']}).",
                    f"Sessions in the last {args.days} days: {result['sessions']}; significant turns {verb}: {result['imported']} "
                    f"(minor and repeated ones skipped: {result['skipped']}).",
                ))
        elif args.cmd == "add-folder":
            result = add_folder(args.folder, args.prefix, args.ext)
            if as_json:
                _print_json(result)
            elif result.get("added"):
                print(L(
                    f"Подключено: {result['path']} → {result['prefix']}. Индекс обновится сам (или сразу: memory sync).",
                    f"Added: {result['path']} → {result['prefix']}. The index will update on its own (or right now: memory sync).",
                ))
            else:
                print(L(f"Уже подключено: {result['path']}.", f"Already added: {result['path']}."))
        elif args.cmd == "remove-folder":
            result = remove_folder(args.folder)
            if as_json:
                _print_json(result)
            elif result.get("removed"):
                print(L("Отключено.", "Removed."))
            else:
                print(L("Такой папки нет в списке.", "This folder is not in the list."))
        elif args.cmd == "embeddings":
            result = set_embeddings(args.state, args.model, args.endpoint)
            if as_json:
                _print_json(result)
            elif not result["enabled"]:
                print(L("Смысловой поиск выключен: ищу только по словам.", "Semantic search is off: searching by keywords only."))
            elif result.get("check") == "ok":
                print(L(
                    f"Смысловой поиск включён ({result['model']}), проверка прошла.",
                    f"Semantic search is on ({result['model']}), the check passed.",
                ))
            else:
                print(L(
                    f"Смысловой поиск включён ({result['model']}), но проверка не прошла: {result.get('check')}",
                    f"Semantic search is on ({result['model']}), but the check failed: {result.get('check')}",
                ))
            if result["enabled"] and result.get("check") != "ok":
                return 3
        elif args.cmd == "context":
            print(build_session_context(args.max_chars))
        elif args.cmd == "doctor":
            checks, healthy = doctor()
            if as_json:
                _print_json({"healthy": healthy, "checks": [{"ok": ok, "text": text} for ok, text in checks]})
            else:
                for ok, text in checks:
                    print(f"{'✓' if ok else '✗'} {text}")
                print(
                    L("\nВсё в порядке.", "\nAll good.")
                    if healthy
                    else L(
                        "\nЕсть что поправить — см. строки с ✗ и раздел «Если что-то не работает» в README.",
                        "\nSomething needs fixing — see the lines with ✗ and the \"Troubleshooting\" section in the README.",
                    )
                )
            return 0 if healthy else 1
        elif args.cmd == "lang":
            if args.code:
                return set_language(args.code)
            name, other = _language()
            print(L(f"Язык: {name}. Сменить: memory lang {other}", f"Language: {name}. Switch: memory lang {other}"))
    except (ValueError, OSError, sqlite3.Error) as exc:
        warn(L(f"ошибка: {exc}", f"error: {exc}"))
        return 1
    return 0


def text_only_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "text_only", False))


if __name__ == "__main__":
    sys.exit(main())
