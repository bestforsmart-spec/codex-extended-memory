#!/usr/bin/env python3
"""Язык пакета: русский или английский.

Выбор хранится в <память>/.state/lang. Порядок для работающей памяти: переменная
CODEX_MEMORY_LANG → сохранённый выбор → русский (так ведут себя установки, сделанные
до версии 1.2.0, когда выбора языка ещё не было). Установщик выбирает язык через
pick_for_install() и сохраняет его.

Сообщения в коде записаны парой: L("по-русски", "in English").
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

SUPPORTED = ("ru", "en")
FALLBACK = "ru"
ENV_VAR = "CODEX_MEMORY_LANG"
NAMES = {"ru": "русский", "en": "English"}
_ALIASES = {
    "russian": "ru", "rus": "ru", "русский": "ru", "рус": "ru",
    "english": "en", "eng": "en", "английский": "en", "англ": "en",
}

_current: Optional[str] = None


def normalize(value: object) -> Optional[str]:
    """«ru», «RU», «ru_RU.UTF-8», «en-US», «russian» → «ru» или «en»; всё непонятное → None."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    head = text.replace("-", "_").split(".")[0].split("@")[0].split("_")[0]
    return head if head in SUPPORTED else None


def memory_home() -> Path:
    env = os.environ.get("CODEX_MEMORY_HOME", "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    return Path(__file__).resolve().parents[1]


def lang_file(home: Optional[Path] = None) -> Path:
    return (home or memory_home()) / ".state" / "lang"


def saved(home: Optional[Path] = None) -> Optional[str]:
    try:
        return normalize(lang_file(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def save(lang: str, home: Optional[Path] = None) -> str:
    code = normalize(lang)
    if code is None:
        raise ValueError(f"unsupported language: {lang!r} (ru | en)")
    path = lang_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code + "\n", encoding="utf-8")
    return code


def system_lang() -> Optional[str]:
    """Язык системы: «ru» для русской, «en» для любой другой; None, если понять не удалось."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "").strip()
        if value and value.split(".")[0].upper() not in ("C", "POSIX"):
            return normalize(value) or "en"
    if os.name == "nt":
        try:
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            return "ru" if (lang_id & 0x3FF) == 0x19 else "en"  # 0x19 — LANG_RUSSIAN
        except Exception:
            return None
    if sys.platform == "darwin":
        try:
            import subprocess

            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"],
                stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=5,
            ).stdout.strip()
            return (normalize(out) or "en") if out else None
        except Exception:
            return None
    return None


def pick_for_install(explicit: Optional[str], home: Path, legacy: bool) -> str:
    """Язык для установщика.

    Флаг --lang → переменная CODEX_MEMORY_LANG → сохранённый выбор → русский, если память
    поставлена версией без выбора языка (legacy) → язык системы → английский.
    """
    if explicit:
        code = normalize(explicit)
        if code is None:
            raise ValueError(f"unsupported language: {explicit!r} (ru | en)")
        return code
    return normalize(os.environ.get(ENV_VAR)) or saved(home) or ("ru" if legacy else (system_lang() or "en"))


def current() -> str:
    global _current
    if _current is None:
        _current = normalize(os.environ.get(ENV_VAR)) or saved() or FALLBACK
    return _current


def use(lang: str) -> str:
    """Задать язык на время работы процесса: установщик знает его раньше, чем появится файл выбора."""
    global _current
    code = normalize(lang)
    if code is None:
        raise ValueError(f"unsupported language: {lang!r} (ru | en)")
    _current = code
    return code


def L(ru: str, en: str) -> str:
    """Сообщение на текущем языке."""
    return en if current() == "en" else ru


if __name__ == "__main__":  # python3 i18n.py — показать, какой язык выбран и какой у системы
    print(f"current={current()} saved={saved()} system={system_lang()}")
