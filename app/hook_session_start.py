#!/usr/bin/env python3
"""Хук SessionStart: подмешивает главную память (MEMORY.md + свежие записи дневника) в начало сессии.

Никогда не мешает Кодексу: при любой ошибке молча выходит с кодом 0.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    try:
        sys.stdin.buffer.read()  # вход хука нам не нужен, но его надо вычитать (байтами: на Windows stdin не в UTF-8)
    except Exception:
        pass
    import hybrid_memory as hm

    hm.fix_stdio()
    context = hm.build_session_context()
    hm.record_hook_run("SessionStart")
    output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
