#!/usr/bin/env python3
"""Подключает (и отключает) расширенную память в Кодексе.

Правит три файла в ~/.codex (или $CODEX_HOME), перед правкой кладёт копии в <память>/backups/:
  config.toml — MCP-сервер extended-memory (+ features.hooks = true, если ставим хуки);
  hooks.json  — хуки SessionStart и Stop;
  AGENTS.md   — блок инструкций между метками extended-memory:start/end.
Повторный запуск ничего не дублирует; чужие настройки не трогаются.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVER = "extended-memory"
TOML_BEGIN = "# >>> extended-memory (расширенная память) — управляется установщиком >>>"
TOML_END = "# <<< extended-memory <<<"
AGENTS_BEGIN = "<!-- extended-memory:start -->"
AGENTS_END = "<!-- extended-memory:end -->"
AUTO_APPROVE_TOOLS = ("memory_remember", "memory_curate", "memory_sync")
HOOKS = (
    ("SessionStart", "hook_session_start.py", 20, "Подключаю расширенную память"),
    ("Stop", "hook_stop.py", 30, "Сохраняю в расширенную память"),
)

_header_re = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")
_array_header_re = re.compile(r"^\s*\[\[\s*([^\[\]]+?)\s*\]\]\s*(#.*)?$")


def say(text: str) -> None:
    print(text, flush=True)


def fix_stdio() -> None:
    """Windows: вывод в трубу или файл идёт в кодировке системы (cp1251) и падает на «✓» — переводим в UTF-8."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write(path: Path, text: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    elif path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    for attempt in range(6):  # на Windows файл на мгновение может держать другой процесс
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


def backup(files: List[Path], mem_home: Path) -> Optional[Path]:
    existing = [f for f in files if f.exists()]
    if not existing:
        return None
    base = mem_home / "backups" / time.strftime("%Y%m%d-%H%M%S")
    folder, n = base, 2
    while folder.exists():  # два запуска в одну секунду не должны затирать копию
        folder, n = base.with_name(f"{base.name}-{n}"), n + 1
    folder.mkdir(parents=True)
    for f in existing:
        shutil.copy2(f, folder / f.name)
    return folder


# ---------------------------------------------------------------- config.toml

def _norm_table(name: str) -> str:
    return ".".join(part.strip().strip('"').strip("'") for part in name.split("."))


def scan_toml(lines: List[str]) -> List[Tuple[int, str]]:
    """Заголовки таблиц [x.y] (индекс строки, имя) с учётом многострочных строк \"\"\"…\"\"\"."""
    headers: List[Tuple[int, str]] = []
    in_multi: Optional[str] = None
    for idx, line in enumerate(lines):
        if in_multi is None:
            match = _array_header_re.match(line) or _header_re.match(line)
            if match:
                headers.append((idx, _norm_table(match.group(1))))
                continue
        for quote in ('"""', "'''"):
            count = line.count(quote)
            if in_multi is None and count % 2 == 1:
                in_multi = quote
                break
            if in_multi == quote and count % 2 == 1:
                in_multi = None
                break
    return headers


def remove_server_block(text: str) -> str:
    lines = text.splitlines()
    # 1) наш размеченный кусок целиком
    if TOML_BEGIN in lines:
        start = lines.index(TOML_BEGIN)
        end = lines.index(TOML_END, start) if TOML_END in lines[start:] else len(lines) - 1
        del lines[start:end + 1]
    # 2) страховка: таблицы нашего сервера, если метки кто-то стёр
    headers = scan_toml(lines)
    drop: List[Tuple[int, int]] = []
    for pos, (idx, name) in enumerate(headers):
        if name == f"mcp_servers.{SERVER}" or name.startswith(f"mcp_servers.{SERVER}."):
            end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
            drop.append((idx, end))
    for start, end in reversed(drop):
        del lines[start:end]
    return "\n".join(lines).rstrip("\n") + "\n" if lines else ""


def ensure_hooks_feature(text: str) -> Tuple[str, str]:
    lines = text.splitlines()
    headers = scan_toml(lines)
    first_header = headers[0][0] if headers else len(lines)
    for line in lines[:first_header]:
        if re.match(r"^\s*features\s*(\.|=)", line):
            return text, "features задан в корне файла — включите hooks вручную, если они выключены"
    for pos, (idx, name) in enumerate(headers):
        if name != "features":
            continue
        end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        for line in lines[idx + 1:end]:
            match = re.match(r"^\s*hooks\s*=\s*(\w+)", line)
            if match:
                if match.group(1) == "true":
                    return text, "features.hooks уже включён"
                return text, "ВНИМАНИЕ: в config.toml стоит features.hooks = false — хуки памяти работать не будут"
        lines.insert(idx + 1, "hooks = true")
        return "\n".join(lines).rstrip("\n") + "\n", "включил features.hooks"
    new_text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "[features]\nhooks = true\n"
    return new_text, "добавил [features] hooks = true"


def toml_str(value: str) -> str:
    return json.dumps(value)  # JSON-строка с \uXXXX — валидная basic string в TOML


def server_block(python: str, app_dir: Path, mem_home: Path, with_approvals: bool = True) -> str:
    out = [
        TOML_BEGIN,
        f"[mcp_servers.{SERVER}]",
        f"command = {toml_str(python)}",
        f"args = [{toml_str(str(app_dir / 'mcp_server.py'))}]",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 180",
        "",
        f"[mcp_servers.{SERVER}.env]",
        f"CODEX_MEMORY_HOME = {toml_str(str(mem_home))}",
    ]
    if with_approvals:
        for tool in AUTO_APPROVE_TOOLS:
            out += ["", f"[mcp_servers.{SERVER}.tools.{tool}]", 'approval_mode = "approve"']
    out.append(TOML_END)
    return "\n".join(out) + "\n"


def find_codex_bin() -> Optional[str]:
    candidates = [os.environ.get("CODEX_BIN", "")]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(os.path.join(local, "Programs", "OpenAI", "Codex", "bin", "codex.exe"))
    else:
        candidates += [
            "/Applications/Codex.app/Contents/Resources/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            str(Path.home() / "Applications/Codex.app/Contents/Resources/codex"),
        ]
    candidates.append(shutil.which("codex") or "")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def validate_toml(text: str, codex_home: Path, config_path: Path) -> Tuple[bool, str]:
    """Проверяет config.toml: tomllib (Python 3.11+) и, если найден, сам Кодекс."""
    try:
        import tomllib  # type: ignore[import-not-found]

        try:
            tomllib.loads(text)
        except Exception as exc:
            return False, f"TOML не разбирается: {exc}"
    except ImportError:
        pass
    codex = find_codex_bin()
    if not codex:
        return True, "проверено без Кодекса (бинарник не найден)"
    try:
        proc = subprocess.run(
            [codex, "mcp", "list"],
            env=dict(os.environ, CODEX_HOME=str(codex_home)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",  # явно: на Windows кодировка по умолчанию cp1251, и вывод Кодекса на ней падает
            errors="replace",
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return True, f"проверку Кодексом пропустил: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, f"Кодекс не принял config.toml: {output.strip()[:400]}"
    if SERVER not in output:
        return False, f"Кодекс не видит сервер {SERVER}: {output.strip()[:400]}"
    return True, "Кодекс принял config.toml и видит сервер"


def install_config(codex_home: Path, python: str, app_dir: Path, mem_home: Path, hooks: bool) -> None:
    path = codex_home / "config.toml"
    original = read(path)
    base = remove_server_block(original)
    notes = []
    if hooks:
        base, note = ensure_hooks_feature(base)
        notes.append(note)
    last_error = ""
    for with_approvals in (True, False):
        candidate = base.rstrip("\n") + ("\n\n" if base.strip() else "") + server_block(python, app_dir, mem_home, with_approvals)
        write(path, candidate, mode=0o600 if not original else None)
        ok, message = validate_toml(candidate, codex_home, path)
        if ok:
            say(f"  ✓ config.toml: MCP-сервер {SERVER} прописан ({message}{'; ' + '; '.join(notes) if notes else ''})")
            if not with_approvals:
                say("    (эта версия Кодекса не знает approval_mode — запись в память может спрашивать подтверждение)")
            return
        last_error = message
    if original:
        write(path, original)
    elif path.exists():
        path.unlink()
    raise SystemExit(f"  ✗ config.toml не изменён: {last_error}")


def uninstall_config(codex_home: Path) -> None:
    path = codex_home / "config.toml"
    if not path.exists():
        return
    original = read(path)
    cleaned = remove_server_block(original)
    if cleaned != original:
        write(path, cleaned)
        say(f"  ✓ config.toml: сервер {SERVER} убран")


# ---------------------------------------------------------------- hooks.json

def hook_command(python: str, script: str, windows: Optional[bool] = None) -> str:
    """Команда хука. На macOS и Linux Кодекс отдаёт её оболочке ($SHELL -lc), на Windows — cmd.exe /C."""
    if (os.name == "nt") if windows is None else windows:
        return f'"{python}" "{script}"'  # cmd понимает только двойные кавычки
    return f"{shlex.quote(python)} {shlex.quote(script)}"


def _is_ours(hook: Dict[str, Any], app_dir: Path) -> bool:
    command = str(hook.get("command", ""))
    return any(script in command for _, script, _, _ in HOOKS) and (
        str(app_dir) in command or "codex-memory" in command
    )


def install_hooks(codex_home: Path, python: str, app_dir: Path) -> None:
    path = codex_home / "hooks.json"
    try:
        data = json.loads(read(path)) if path.exists() and read(path).strip() else {}
    except ValueError as exc:
        raise SystemExit(f"  ✗ {path} содержит неверный JSON ({exc}) — поправьте его и запустите установку снова")
    if not isinstance(data, dict):
        data = {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, script, timeout, status in HOOKS:
        wanted = {
            "type": "command",
            "command": hook_command(python, str(app_dir / script)),
            "timeout": timeout,
            "statusMessage": status,
        }
        groups = hooks.setdefault(event, [])
        placed = False
        for group in groups:
            for i, hook in enumerate(group.get("hooks", [])):
                if isinstance(hook, dict) and _is_ours(hook, app_dir):
                    if hook != wanted:
                        group["hooks"][i] = wanted  # на том же месте — чужие хуки не сдвигаются
                        changed = True
                    placed = True
        if not placed:
            groups.append({"hooks": [wanted]})
            changed = True
    if changed:
        write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        say("  ✓ hooks.json: хуки SessionStart и Stop добавлены (Кодекс попросит им доверять — нажмите Trust)")
    else:
        say("  ✓ hooks.json: хуки уже на месте")


def uninstall_hooks(codex_home: Path, app_dir: Path) -> None:
    path = codex_home / "hooks.json"
    if not path.exists():
        return
    try:
        data = json.loads(read(path))
    except ValueError:
        say(f"  ! {path}: неверный JSON, хуки уберите вручную")
        return
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    changed = False
    for event in list(hooks):
        new_groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not (isinstance(h, dict) and _is_ours(h, app_dir))]
            if len(kept) != len(group.get("hooks", [])):
                changed = True
            if kept:
                group["hooks"] = kept
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if changed:
        write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        say("  ✓ hooks.json: хуки памяти убраны")


# ---------------------------------------------------------------- AGENTS.md

def install_agents(codex_home: Path, block_template: Path, mem_home: Path) -> None:
    path = codex_home / "AGENTS.md"
    block = block_template.read_text(encoding="utf-8").strip()
    cli = mem_home / "bin" / ("memory.cmd" if os.name == "nt" else "memory")
    block = block.replace("{{MEMORY_HOME}}", str(mem_home)).replace("{{MEMORY_CLI}}", str(cli))
    block = f"{AGENTS_BEGIN}\n{block}\n{AGENTS_END}"
    text = read(path)
    if AGENTS_BEGIN in text and AGENTS_END in text:
        start = text.index(AGENTS_BEGIN)
        end = text.index(AGENTS_END, start) + len(AGENTS_END)
        new_text = text[:start] + block + text[end:]
    else:
        new_text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    if new_text != text:
        write(path, new_text, mode=0o644 if not text else None)
        say("  ✓ AGENTS.md: инструкции по памяти добавлены")
    else:
        say("  ✓ AGENTS.md: инструкции уже на месте")


def uninstall_agents(codex_home: Path) -> None:
    path = codex_home / "AGENTS.md"
    text = read(path)
    if AGENTS_BEGIN in text and AGENTS_END in text:
        start = text.index(AGENTS_BEGIN)
        end = text.index(AGENTS_END, start) + len(AGENTS_END)
        new_text = (text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")).strip("\n")
        write(path, new_text + "\n" if new_text else "")
        say("  ✓ AGENTS.md: блок памяти убран")


# ---------------------------------------------------------------- main

def main() -> int:
    fix_stdio()
    parser = argparse.ArgumentParser(description="Подключение расширенной памяти к Кодексу")
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--mem-home", required=True)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-hooks", action="store_true")
    args = parser.parse_args()

    mem_home = Path(os.path.expanduser(args.mem_home)).resolve()
    codex_home = Path(os.path.expanduser(args.codex_home)).resolve()
    app_dir = mem_home / "app"
    files = [codex_home / "config.toml", codex_home / "hooks.json", codex_home / "AGENTS.md"]
    folder = backup(files, mem_home)
    if folder:
        say(f"  ✓ копии настроек Кодекса: {folder}")

    if args.action == "install":
        codex_home.mkdir(parents=True, exist_ok=True)
        install_config(codex_home, args.python, app_dir, mem_home, hooks=not args.no_hooks)
        if args.no_hooks:
            uninstall_hooks(codex_home, app_dir)
        else:
            install_hooks(codex_home, args.python, app_dir)
        install_agents(codex_home, app_dir / "templates" / "AGENTS-block.md", mem_home)
    else:
        uninstall_config(codex_home)
        uninstall_hooks(codex_home, app_dir)
        uninstall_agents(codex_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
