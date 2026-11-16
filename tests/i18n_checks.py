#!/usr/bin/env python3
"""Проверки двуязычности пакета (русский и английский). Нужен Python 3.9+.

Статические (без установки):
  - каждая строка с кириллицей в app/*.py — либо первый аргумент L("…", "…"), либо docstring,
    либо значение из списка исключений (стоп-слова, регулярные выражения, метки файлов);
  - у каждого L() два аргумента, во втором нет кириллицы, подстановки в обоих одинаковые;
  - шаблоны есть на обоих языках, в английских нет кириллицы;
  - в install.sh и uninstall.sh каждое русское сообщение стоит в паре с английским.

usage: i18n_checks.py <корень репозитория> [файл.py ...]
       Если перечислены файлы, проверяются только они (имена из app/).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(sys.argv[1]).resolve()
ONLY = set(sys.argv[2:])
CYR = re.compile(r"[а-яёА-ЯЁ]")
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok or not detail else f"\n      {detail}"))
    failures += not ok


# Имена верхнего уровня (переменные и функции), внутри которых кириллица — не сообщение:
# данные для разбора русского текста, метки в файлах пользователя, названия языков.
ALLOW = {
    "i18n.py": {"NAMES", "_ALIASES"},
    "hybrid_memory.py": {
        "STOPWORDS", "_RU_ENDINGS", "_cyrillic_word", "TRIVIAL_RE", "SIGNAL_RE", "SECRET_PATTERNS",
        "CURATED_SECTIONS",
    },
    "configure_codex.py": {"TOML_BEGINS"},
    "mcp_server.py": set(),
    "setup_windows.py": set(),
    "hook_session_start.py": set(),
    "hook_stop.py": set(),
    "run_task.py": set(),
}


def is_L(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "L") or (isinstance(func, ast.Attribute) and func.attr == "L")


def docstrings(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def top_name(stmt: ast.stmt) -> str:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name
    if isinstance(stmt, ast.Assign) and stmt.targets and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return ""


def placeholders(node: ast.AST) -> list:
    """Выражения в фигурных скобках f-строки (и её склеек) — как текст."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.FormattedValue):
            out.append(ast.unparse(sub.value))
    return sorted(out)


def check_python(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = docstrings(tree)
    allow = ALLOW.get(path.name, set())
    loose, bad_pairs = [], []
    inside_L_first = set()
    for node in ast.walk(tree):
        if not is_L(node):
            continue
        if len(node.args) != 2 or node.keywords:
            bad_pairs.append(f"строка {node.lineno}: у L() должно быть ровно два аргумента")
            continue
        ru, en = node.args
        for sub in ast.walk(ru):
            inside_L_first.add(id(sub))
        if any(isinstance(s, ast.Constant) and isinstance(s.value, str) and CYR.search(s.value) for s in ast.walk(en)):
            bad_pairs.append(f"строка {node.lineno}: в английском варианте осталась кириллица")
        if placeholders(ru) != placeholders(en):
            bad_pairs.append(f"строка {node.lineno}: подстановки различаются: {placeholders(ru)} и {placeholders(en)}")
        if not any(isinstance(s, ast.Constant) and isinstance(s.value, str) and CYR.search(s.value) for s in ast.walk(ru)):
            bad_pairs.append(f"строка {node.lineno}: в русском варианте нет русского текста")
    for stmt in tree.body:
        name = top_name(stmt)
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CYR.search(node.value):
                if id(node) in docs or id(node) in inside_L_first or name in allow:
                    continue
                loose.append(f"строка {node.lineno} ({name or 'модуль'}): {node.value[:70]!r}")
    check(f"{path.name}: все русские сообщения стоят в L()", not loose, "; ".join(loose[:12]) + (f" … ещё {len(loose) - 12}" if len(loose) > 12 else ""))
    check(f"{path.name}: пары L() согласованы", not bad_pairs, "; ".join(bad_pairs[:12]))


print("Двуязычность: код")
for path in sorted((ROOT / "app").glob("*.py")):
    if ONLY and path.name not in ONLY:
        continue
    check_python(path)

if not ONLY:
    print("Двуязычность: шаблоны")
    ru_dir, en_dir = ROOT / "app" / "templates" / "ru", ROOT / "app" / "templates" / "en"
    ru_names = sorted(p.name for p in ru_dir.glob("*")) if ru_dir.is_dir() else []
    en_names = sorted(p.name for p in en_dir.glob("*")) if en_dir.is_dir() else []
    check("шаблоны есть на обоих языках", bool(ru_names) and ru_names == en_names, f"ru: {ru_names}; en: {en_names}")
    for name in en_names:
        text = (en_dir / name).read_text(encoding="utf-8")
        check(f"templates/en/{name}: без кириллицы", not CYR.search(text))
    for lang_dir in (ru_dir, en_dir):
        block = lang_dir / "AGENTS-block.md"
        if block.exists():
            check(f"templates/{lang_dir.name}/AGENTS-block.md: есть {{{{MEMORY_CLI}}}}", "{{MEMORY_CLI}}" in block.read_text(encoding="utf-8"))

    print("Двуязычность: установщики bash")
    pair = re.compile(r"""\b(?:m|say)\s+(?:'[^']*'|"(?:[^"\\]|\\.)*")\s+(?:'([^']*)'|"((?:[^"\\]|\\.)*)")""")
    for script in ("install.sh", "uninstall.sh"):
        loose = []
        for no, line in enumerate((ROOT / script).read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") or not CYR.search(line):
                continue
            found = pair.search(line)
            english = (found.group(1) or found.group(2) or "") if found else ""
            if not found or CYR.search(english) or not english.strip():
                loose.append(f"строка {no}")
        check(f"{script}: у каждого русского сообщения есть английская пара", not loose, ", ".join(loose[:20]))

print("\nИтог:", "все проверки пройдены" if not failures else f"не пройдено проверок: {failures}")
sys.exit(1 if failures else 0)
