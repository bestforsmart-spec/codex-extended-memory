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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n  # noqa: E402
from i18n import L  # noqa: E402

SERVER = "extended-memory"
# Первая строка нашего куска в config.toml: по ней он находится при удалении, на каком бы языке ни ставился.
TOML_BEGINS = (
    "# >>> extended-memory (расширенная память) — управляется установщиком >>>",
    "# >>> extended-memory — managed by the installer >>>",
)
TOML_END = "# <<< extended-memory <<<"
AGENTS_BEGIN = "<!-- extended-memory:start -->"
AGENTS_END = "<!-- extended-memory:end -->"
AUTO_APPROVE_TOOLS = ("memory_remember", "memory_curate", "memory_sync")
HOOK_SCRIPTS = ("hook_session_start.py", "hook_stop.py")


def toml_begin() -> str:
    return TOML_BEGINS[1] if i18n.current() == "en" else TOML_BEGINS[0]


def hooks_spec() -> Tuple[Tuple[str, str, int, str], ...]:
    """Событие, скрипт, тайм-аут и подпись, которую Кодекс показывает, пока хук работает."""
    return (
        ("SessionStart", "hook_session_start.py", 20, L("Подключаю расширенную память", "Loading extended memory")),
        ("Stop", "hook_stop.py", 30, L("Сохраняю в расширенную память", "Saving to extended memory")),
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
    for begin in TOML_BEGINS:
        if begin in lines:
            start = lines.index(begin)
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
            return text, L(
                "features задан в корне файла — включите hooks вручную, если они выключены",
                "features is set at the top level of the file — turn hooks on by hand if they are off",
            )
    for pos, (idx, name) in enumerate(headers):
        if name != "features":
            continue
        end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        for line in lines[idx + 1:end]:
            match = re.match(r"^\s*hooks\s*=\s*(\w+)", line)
            if match:
                if match.group(1) == "true":
                    return text, L("features.hooks уже включён", "features.hooks is already on")
                return text, L(
                    "ВНИМАНИЕ: в config.toml стоит features.hooks = false — хуки памяти работать не будут",
                    "WARNING: config.toml has features.hooks = false — the memory hooks will not run",
                )
        lines.insert(idx + 1, "hooks = true")
        return "\n".join(lines).rstrip("\n") + "\n", L("включил features.hooks", "turned on features.hooks")
    new_text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "[features]\nhooks = true\n"
    return new_text, L("добавил [features] hooks = true", "added [features] hooks = true")


def toml_str(value: str) -> str:
    return json.dumps(value)  # JSON-строка с \uXXXX — валидная basic string в TOML


def server_block(python: str, app_dir: Path, mem_home: Path, with_approvals: bool = True) -> str:
    out = [
        toml_begin(),
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
            return False, L(f"TOML не разбирается: {exc}", f"TOML does not parse: {exc}")
    except ImportError:
        pass
    codex = find_codex_bin()
    if not codex:
        return True, L("проверено без Кодекса (бинарник не найден)", "checked without Codex (binary not found)")
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
        return True, L(f"проверку Кодексом пропустил: {exc}", f"skipped the check by Codex: {exc}")
    output = (proc.stdout or "") + (proc.stderr or "")
    shown = output.strip()[:400]
    if proc.returncode != 0:
        return False, L(f"Кодекс не принял config.toml: {shown}", f"Codex rejected config.toml: {shown}")
    if SERVER not in output:
        return False, L(f"Кодекс не видит сервер {SERVER}: {shown}", f"Codex does not see the {SERVER} server: {shown}")
    return True, L("Кодекс принял config.toml и видит сервер", "Codex accepted config.toml and sees the server")


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
            extra = "; " + "; ".join(notes) if notes else ""
            say(L(
                f"  ✓ config.toml: MCP-сервер {SERVER} прописан ({message}{extra})",
                f"  ✓ config.toml: MCP server {SERVER} registered ({message}{extra})",
            ))
            if not with_approvals:
                say(L(
                    "    (эта версия Кодекса не знает approval_mode — запись в память может спрашивать подтверждение)",
                    "    (this Codex version does not know approval_mode — writing to memory may ask for confirmation)",
                ))
            return
        last_error = message
    if original:
        write(path, original)
    elif path.exists():
        path.unlink()
    raise SystemExit(L(f"  ✗ config.toml не изменён: {last_error}", f"  ✗ config.toml left unchanged: {last_error}"))


def uninstall_config(codex_home: Path) -> None:
    path = codex_home / "config.toml"
    if not path.exists():
        return
    original = read(path)
    cleaned = remove_server_block(original)
    if cleaned != original:
        write(path, cleaned)
        say(L(f"  ✓ config.toml: сервер {SERVER} убран", f"  ✓ config.toml: server {SERVER} removed"))


# ---------------------------------------------------------------- hooks.json

def hook_command(python: str, script: str, windows: Optional[bool] = None) -> str:
    """Команда хука. На macOS и Linux Кодекс отдаёт её оболочке ($SHELL -lc), на Windows — cmd.exe /C."""
    if (os.name == "nt") if windows is None else windows:
        return f'"{python}" "{script}"'  # cmd понимает только двойные кавычки
    return f"{shlex.quote(python)} {shlex.quote(script)}"


def _is_ours(hook: Dict[str, Any], app_dir: Path) -> bool:
    command = str(hook.get("command", ""))
    return any(script in command for script in HOOK_SCRIPTS) and (
        str(app_dir) in command or "codex-memory" in command
    )


def install_hooks(codex_home: Path, python: str, app_dir: Path) -> None:
    path = codex_home / "hooks.json"
    try:
        data = json.loads(read(path)) if path.exists() and read(path).strip() else {}
    except ValueError as exc:
        raise SystemExit(L(
            f"  ✗ {path} содержит неверный JSON ({exc}) — поправьте его и запустите установку снова",
            f"  ✗ {path} contains invalid JSON ({exc}) — fix it and run the installer again",
        ))
    if not isinstance(data, dict):
        data = {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, script, timeout, status in hooks_spec():
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
        say(L(
            "  ✓ hooks.json: хуки SessionStart и Stop добавлены (Кодекс попросит им доверять — нажмите Trust)",
            "  ✓ hooks.json: SessionStart and Stop hooks added (Codex will ask you to trust them — choose Trust)",
        ))
    else:
        say(L("  ✓ hooks.json: хуки уже на месте", "  ✓ hooks.json: hooks are already in place"))


def uninstall_hooks(codex_home: Path, app_dir: Path) -> None:
    path = codex_home / "hooks.json"
    if not path.exists():
        return
    try:
        data = json.loads(read(path))
    except ValueError:
        say(L(f"  ! {path}: неверный JSON, хуки уберите вручную", f"  ! {path}: invalid JSON, remove the hooks by hand"))
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
        say(L("  ✓ hooks.json: хуки памяти убраны", "  ✓ hooks.json: memory hooks removed"))


# ---------------------------------------------------------------- AGENTS.md

def agents_template(app_dir: Path) -> Path:
    """Инструкции для агента на текущем языке."""
    return app_dir / "templates" / i18n.current() / "AGENTS-block.md"


def install_agents(codex_home: Path, block_template: Path, mem_home: Path) -> None:
    path = codex_home / "AGENTS.md"
    block = block_template.read_text(encoding="utf-8").strip()
    cli = mem_home / "bin" / ("memory.cmd" if os.name == "nt" else "memory")
    block = block.replace("{{MEMORY_HOME}}", str(mem_home)).replace("{{MEMORY_CLI}}", str(cli))
    block = f"{AGENTS_BEGIN}\n{block}\n{AGENTS_END}"
    text = read(path)
    had_block = AGENTS_BEGIN in text and AGENTS_END in text
    if had_block:
        start = text.index(AGENTS_BEGIN)
        end = text.index(AGENTS_END, start) + len(AGENTS_END)
        new_text = text[:start] + block + text[end:]
    else:
        new_text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    if new_text == text:
        say(L("  ✓ AGENTS.md: инструкции уже на месте", "  ✓ AGENTS.md: instructions are already in place"))
        return
    write(path, new_text, mode=0o644 if not text else None)
    if had_block:
        say(L("  ✓ AGENTS.md: инструкции по памяти обновлены", "  ✓ AGENTS.md: memory instructions updated"))
    else:
        say(L("  ✓ AGENTS.md: инструкции по памяти добавлены", "  ✓ AGENTS.md: memory instructions added"))


def uninstall_agents(codex_home: Path) -> None:
    path = codex_home / "AGENTS.md"
    text = read(path)
    if AGENTS_BEGIN in text and AGENTS_END in text:
        start = text.index(AGENTS_BEGIN)
        end = text.index(AGENTS_END, start) + len(AGENTS_END)
        new_text = (text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")).strip("\n")
        write(path, new_text + "\n" if new_text else "")
        say(L("  ✓ AGENTS.md: блок памяти убран", "  ✓ AGENTS.md: memory block removed"))


# ---------------------------------------------------------------- main

def _argument(argv: List[str], name: str) -> Optional[str]:
    """Значение параметра «--name X» или «--name=X» до разбора argparse: язык нужен уже для текста справки."""
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def main() -> int:
    fix_stdio()
    argv = sys.argv[1:]
    # Язык: флаг --lang → переменная CODEX_MEMORY_LANG → выбор, сохранённый в папке памяти → русский.
    home_arg = _argument(argv, "--mem-home")
    home = Path(os.path.expanduser(home_arg)).resolve() if home_arg else None
    lang = (
        i18n.normalize(_argument(argv, "--lang"))
        or i18n.normalize(os.environ.get(i18n.ENV_VAR))
        or i18n.saved(home)
        or i18n.FALLBACK
    )
    i18n.use(lang)

    parser = argparse.ArgumentParser(description=L("Подключение расширенной памяти к Кодексу", "Connects extended memory to Codex"))
    parser.add_argument("action", choices=["install", "uninstall", "relang"])
    parser.add_argument("--mem-home", required=True)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-hooks", action="store_true")
    parser.add_argument("--lang", choices=list(i18n.SUPPORTED))
    args = parser.parse_args()

    mem_home = Path(os.path.expanduser(args.mem_home)).resolve()
    codex_home = Path(os.path.expanduser(args.codex_home)).resolve()
    app_dir = mem_home / "app"
    files = [codex_home / "config.toml", codex_home / "hooks.json", codex_home / "AGENTS.md"]
    if args.action == "relang":
        files = [codex_home / "AGENTS.md"]  # при смене языка меняются только инструкции для агента
    folder = backup(files, mem_home)
    if folder:
        say(L(f"  ✓ копии настроек Кодекса: {folder}", f"  ✓ copies of the Codex settings: {folder}"))

    if args.action == "install":
        codex_home.mkdir(parents=True, exist_ok=True)
        install_config(codex_home, args.python, app_dir, mem_home, hooks=not args.no_hooks)
        if args.no_hooks:
            uninstall_hooks(codex_home, app_dir)
        else:
            install_hooks(codex_home, args.python, app_dir)
        install_agents(codex_home, agents_template(app_dir), mem_home)
    elif args.action == "relang":
        # Подписи хуков в hooks.json намеренно не трогаем: после их правки Кодекс заново просит
        # доверять хукам, и пока пользователь не подтвердит, память не подмешивается и не сохраняется.
        codex_home.mkdir(parents=True, exist_ok=True)
        install_agents(codex_home, agents_template(app_dir), mem_home)
    else:
        uninstall_config(codex_home)
        uninstall_hooks(codex_home, app_dir)
        uninstall_agents(codex_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
