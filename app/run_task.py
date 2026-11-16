#!/usr/bin/env python3
"""Фоновая задача памяти без окна консоли: run_task.py sync --quiet

Нужна на Windows: Планировщик заданий запускает её через pythonw.exe, у которого нет ни консоли,
ни потоков вывода. Вывод команды дописывается в <память>/logs/<команда>.log.
"""
from __future__ import annotations

import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    args = sys.argv[1:] or ["sync", "--quiet"]
    home = os.environ.get("CODEX_MEMORY_HOME") or os.path.dirname(APP_DIR)
    os.environ["CODEX_MEMORY_HOME"] = home
    log_dir = os.path.join(home, "logs")
    os.makedirs(log_dir, exist_ok=True)
    name = "".join(ch for ch in args[0] if ch.isalnum() or ch in "-_") or "task"
    log = open(os.path.join(log_dir, f"{name}.log"), "a", encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = log
    sys.path.insert(0, APP_DIR)
    try:
        from i18n import L
    except Exception:  # i18n.py не нашёлся или не читается: об ошибке ниже сообщаем по-русски

        def L(ru: str, en: str) -> str:
            return ru

    try:
        import hybrid_memory as hm

        code = hm.main(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # фоновая задача не должна показывать окно с ошибкой
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(L(f"{stamp} ошибка: {exc!r}", f"{stamp} error: {exc!r}"))
        code = 1
    finally:
        log.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
