#!/usr/bin/env python3
"""Живые проверки памяти — на настоящей системе, вокруг настоящей установки.

usage:
  live_checks.py prepare   --codex-home DIR
      кладёт поддельную сессию Кодекса, чтобы при установке проверилась загрузка истории
  live_checks.py installed --memory-home DIR --codex-home DIR [--lang ru|en]
                           [--install-log FILE] [--expect-codex] [--expect-schedule] [--expect-history]
                           [--expect-embeddings]
      проверяет установку: файлы, настройки Кодекса, командную строку, хуки (так, как их запускает
      Кодекс), сервер MCP, замок индекса, фоновые задачи, смену языка. Язык берётся из
      <память>/.state/lang; с --lang дополнительно проверяется, что установщик выбрал именно его
  live_checks.py removed   --memory-home DIR --codex-home DIR [--purged]
      проверяет, что удаление убрало своё и оставило заметки

Только стандартная библиотека, Python 3.8+. Код возврата 1, если хоть одна проверка не прошла.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
FAILS: List[str] = []

for _stream in (sys.stdout, sys.stderr):  # на Windows вывод в трубу идёт не в UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

HISTORY_USER = "Перенеси отчёты по продажам на новый сервер и проверь, что выгрузка работает"
HISTORY_ASSISTANT = (
    "Перенёс отчёты на новый сервер: выгрузка настроена через планировщик, старые файлы сохранены в архиве. "
    "Проверил три последних отчёта — цифры сходятся с исходными. Причина прошлых сбоев была в кодировке имён "
    "файлов, теперь она исправлена. Следующий шаг — отключить старый сервер после недели параллельной работы."
)
HISTORY_PROJECT = "сайт-магазина"
STOP_USER = "Настрой резервное копирование базы клиентов в кипарисовый архив"
STOP_ASSISTANT = (
    "Настроил резервное копирование базы клиентов: копия снимается каждую ночь и складывается в кипарисовый "
    "архив на втором диске. Исправил права на папку, из-за которых копия раньше не создавалась. Решили хранить "
    "тридцать последних копий: этого хватает, а места занимает немного. Проверил восстановление на вчерашней "
    "копии — данные на месте."
)
STOP_WORD = "кипарисовый"
STOP_PROJECT = "проект памяти"
MCP_NOTE = "Поставщику «Берёзка» платим в течение двадцати дней, счёт присылает бухгалтер Лаврентьева."
MCP_WORD = "Лаврентьева"
SEMANTIC_NOTE = "Рассылку отправляем через SMTP-релей: при прямой отправке почта оседала в папке нежелательных сообщений."
SEMANTIC_QUERY = "письма попадают в спам"
SEMANTIC_MARK = "SMTP-релей"

# Что память говорит на каждом из языков: по этим словам проверяется язык установки.
TEXT: Dict[str, Dict[str, str]] = {
    "ru": {
        "status_files": "Файлов в индексе",
        "status_title": "Расширенная память",
        "saved": "Сохранено",
        "context_word": "главная память",
        "auto_label": "запрос:",
        "doctor_ok": "Всё в порядке",
        "doctor_emb": "эмбеддинги работают",
        "doctor_lang": "язык: русский",
        "lang_line": "Язык: русский",
        "log_done": "Готово!",
        "log_title": "Расширенная память для Кодекса",
        "log_codex_ok": "Кодекс принял config.toml",
        "log_no_codex": "проверено без Кодекса",
        "agents_title": "## Расширенная память",
        "memory_about": "## Обо мне",
    },
    "en": {
        "status_files": "Files in the index",
        "status_title": "Extended memory",
        "saved": "Saved",
        "context_word": "main memory",
        "auto_label": "request:",
        "doctor_ok": "All good",
        "doctor_emb": "embeddings work",
        "doctor_lang": "language: English",
        "lang_line": "Language: English",
        "log_done": "Done!",
        "log_title": "Extended Memory for Codex",
        "log_codex_ok": "Codex accepted config.toml",
        "log_no_codex": "checked without Codex",
        "agents_title": "## Extended memory",
        "memory_about": "## About me",
    },
}
LANG = "ru"
T = TEXT[LANG]
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

WINDOWS_ENV = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE", "USERNAME", "USERDOMAIN",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "TEMP", "TMP",
)
POSIX_ENV = ("HOME", "PATH", "SHELL", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")


# ---------------------------------------------------------------- вспомогательное

def section(title: str) -> None:
    print(f"\n== {title}", flush=True)


def info(text: str) -> None:
    print(f"     {text}", flush=True)


def check(name: str, ok: Any, detail: str = "") -> bool:
    ok = bool(ok)
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     ↳ {detail}"), flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def short(text: str, limit: int = 600) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def agent_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Среда, близкая к той, что агент даёт своим дочерним процессам: без CODEX_MEMORY_HOME и PYTHONUTF8."""
    wanted = WINDOWS_ENV if IS_WINDOWS else POSIX_ENV
    env = {key: value for key, value in os.environ.items() if key.upper() in wanted}
    env.update(extra or {})
    return env


def cli_path(mem_home: Path) -> Path:
    return mem_home / "bin" / ("memory.cmd" if IS_WINDOWS else "memory")


def run_cli(mem_home: Path, codex_home: Path, *args: str, timeout: int = 300) -> Tuple[int, str, str]:
    """Запускает командную строку памяти так, как это сделал бы человек в терминале."""
    env = agent_env({"CODEX_HOME": str(codex_home)})
    cli = str(cli_path(mem_home))
    command: Any = [cli, *args]
    if IS_WINDOWS:  # "путь к memory.cmd" аргументы — одной строкой, как в окне cmd
        command = f'cmd.exe /d /s /c ""{cli}" {subprocess.list2cmdline(list(args))}"'
    proc = subprocess.run(command, capture_output=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def wait_no_lock(mem_home: Path, seconds: int = 90) -> bool:
    lock = mem_home / ".index" / "sync.lock"
    deadline = time.time() + seconds
    while lock.exists() and time.time() < deadline:
        time.sleep(0.5)
    return not lock.exists()


def server_entry(codex_home: Path) -> Dict[str, Any]:
    """Запись сервера памяти из config.toml: значения там записаны как JSON-строки."""
    text = read_text(codex_home / "config.toml")
    block = text[text.index("# >>> extended-memory"): text.index("# <<< extended-memory <<<")]

    def value(key: str) -> Any:
        match = re.search(r"^%s\s*=\s*(.+?)\s*$" % re.escape(key), block, re.M)
        return json.loads(match.group(1)) if match else None

    return {"command": value("command"), "args": value("args"), "home": value("CODEX_MEMORY_HOME")}


def hook_commands(codex_home: Path) -> Dict[str, str]:
    data = json.loads(read_text(codex_home / "hooks.json"))
    found: Dict[str, str] = {}
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = str(hook.get("command", ""))
                if "hook_session_start.py" in command or "hook_stop.py" in command:
                    found[event] = command
    return found


def run_hook(command: str, payload: Dict[str, Any], cwd: Path) -> subprocess.CompletedProcess:
    """Запускает хук так же, как Кодекс: на Windows — cmd.exe /C "команда", иначе — $SHELL -lc команда."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if IS_WINDOWS:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        line = f'"{comspec}" /C "{command}"'
        return subprocess.run(line, input=data, capture_output=True, cwd=str(cwd), env=agent_env(), timeout=120)
    shell = os.environ.get("SHELL") or "/bin/sh"
    return subprocess.run([shell, "-lc", command], input=data, capture_output=True, cwd=str(cwd), env=agent_env(), timeout=120)


class McpClient:
    """Клиент MCP по stdio: одна строка JSON на сообщение, строго в UTF-8."""

    def __init__(self, command: str, args: List[str], env: Dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.errors: List[bytes] = []
        self.next_id = 0
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.lines.put(raw)
        self.lines.put(None)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.errors.append(raw)

    def _send(self, message: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: int = 180) -> Dict[str, Any]:
        self.next_id += 1
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        while True:
            raw = self.lines.get(timeout=timeout)
            if raw is None:
                raise RuntimeError("сервер закрыл вывод: " + short(b"".join(self.errors).decode("utf-8", "replace")))
            reply = json.loads(raw.decode("utf-8"))
            if reply.get("id") == self.next_id:
                return reply

    def tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        result = reply.get("result") or {}
        text = "".join(part.get("text", "") for part in result.get("content", []))
        return (not result.get("isError", False)) and "error" not in reply, text

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


def open_client(entry: Dict[str, Any], name: str) -> McpClient:
    client = McpClient(entry["command"], entry["args"], agent_env({"CODEX_MEMORY_HOME": entry["home"]}))
    reply = client.request(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": name, "version": "1"}},
    )
    client.server_info = reply.get("result", {}).get("serverInfo", {})  # type: ignore[attr-defined]
    client.notify("notifications/initialized")
    return client


# ---------------------------------------------------------------- prepare

def prepare(codex_home: Path) -> int:
    folder = codex_home / "sessions" / time.strftime("%Y") / time.strftime("%m") / time.strftime("%d")
    folder.mkdir(parents=True, exist_ok=True)
    cwd = "C:\\Users\\Тест\\Проекты\\" + HISTORY_PROJECT if IS_WINDOWS else "/home/test/проекты/" + HISTORY_PROJECT
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = [
        {"timestamp": now, "type": "session_meta", "payload": {"cwd": cwd}},
        {"timestamp": now, "type": "event_msg", "payload": {"type": "user_message", "message": HISTORY_USER}},
        {"timestamp": now, "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "live-check-history", "last_agent_message": HISTORY_ASSISTANT}},
    ]
    path = folder / "rollout-live-check.jsonl"
    path.write_bytes("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8"))
    print(f"поддельная сессия Кодекса: {path}")
    return 0


# ---------------------------------------------------------------- installed

def check_layout(mem_home: Path) -> None:
    section("Файлы")
    names = ["app/hybrid_memory.py", "app/mcp_server.py", "app/configure_codex.py", "app/i18n.py",
             "app/templates/ru/MEMORY.md", "app/templates/en/MEMORY.md",
             "app/templates/ru/AGENTS-block.md", "app/templates/en/AGENTS-block.md",
             "MEMORY.md", "README.md", "README.ru.md", "index-roots.json"]
    names += ["bin/memory.cmd", "uninstall.cmd", "app/run_task.py"] if IS_WINDOWS else ["bin/memory", "uninstall.sh"]
    for name in names:
        check(f"есть {name}", (mem_home / name).is_file())
    check("MEMORY.md: разделы на языке установки", T["memory_about"] in read_text(mem_home / "MEMORY.md"))
    if IS_WINDOWS:
        data = (mem_home / "bin" / "memory.cmd").read_bytes()
        check("memory.cmd: только ASCII и CRLF", all(b < 128 for b in data) and b"\n" not in data.replace(b"\r\n", b""))


def check_codex_config(mem_home: Path, codex_home: Path) -> Dict[str, Any]:
    section("Настройки Кодекса")
    entry = server_entry(codex_home)
    check("config.toml: путь к Python существует", entry["command"] and Path(entry["command"]).is_file(), str(entry["command"]))
    check("config.toml: сервер — mcp_server.py из папки памяти",
          entry["args"] and Path(entry["args"][0]) == mem_home / "app" / "mcp_server.py", str(entry["args"]))
    check("config.toml: CODEX_MEMORY_HOME — папка памяти", entry["home"] and Path(entry["home"]) == mem_home, str(entry["home"]))
    try:
        import tomllib  # type: ignore[import-not-found]

        parsed = tomllib.loads(read_text(codex_home / "config.toml"))
        check("config.toml разбирается как TOML", parsed["mcp_servers"]["extended-memory"]["command"] == entry["command"])
    except ImportError:
        info("tomllib нет (Python < 3.11) — разбор TOML пропущен")
    hooks = hook_commands(codex_home)
    check("hooks.json: хуки SessionStart и Stop", set(hooks) == {"SessionStart", "Stop"}, str(hooks))
    for event, command in hooks.items():
        info(f"{event}: {command}")
    agents = read_text(codex_home / "AGENTS.md")
    check("AGENTS.md: блок инструкций", "<!-- extended-memory:start -->" in agents and "memory_search" in agents)
    check("AGENTS.md: путь к командной строке", str(cli_path(mem_home)) in agents)
    check("AGENTS.md: инструкции на языке установки", T["agents_title"] in agents, short(agents, 200))
    return entry


def check_history(mem_home: Path) -> None:
    section("История сессий Кодекса")
    files = sorted((mem_home / "memory" / "history").glob("*.md"))
    text = "\n".join(read_text(path) for path in files)
    check("история загружена в memory/history", bool(files), "файлов нет")
    check("в истории есть запрос и итог на русском", HISTORY_USER in text and "параллельной работы" in text, short(text, 300))
    check("в истории имя проекта из пути", f"`{HISTORY_PROJECT}`" in text, short(text, 300))


def check_cli(mem_home: Path, codex_home: Path) -> None:
    section("Командная строка")
    code, out, err = run_cli(mem_home, codex_home, "status", "--json")
    try:
        status = json.loads(out)
    except ValueError:
        status = {}
    check("memory status --json", code == 0 and status.get("files", 0) > 0, short(out + err))
    code, out, err = run_cli(mem_home, codex_home, "status")
    check("memory status: текст на языке установки, без искажений", code == 0 and T["status_files"] in out, short(out + err))
    code, out, err = run_cli(mem_home, codex_home, "--version")
    check("memory --version", code == 0 and re.search(r"\d+\.\d+\.\d+", out + err), short(out + err))
    info(short(out + err, 80))
    code, out, err = run_cli(mem_home, codex_home, "remember", "Проверочная запись: договор с «Альфой» продлён до марта")
    check("memory remember с русским текстом и кавычками", code == 0 and T["saved"] in out, short(out + err))
    code, out, err = run_cli(mem_home, codex_home, "search", "договор Альфа")
    check("memory search находит запись", code == 0 and "продлён до марта" in out, short(out + err))


def check_hooks(mem_home: Path, codex_home: Path) -> None:
    section("Хуки — так, как их запускает Кодекс")
    hooks = hook_commands(codex_home)
    work = Path(tempfile.mkdtemp(prefix="live-check-")) / STOP_PROJECT
    work.mkdir(parents=True)

    start = run_hook(hooks["SessionStart"], {"session_id": "live-check", "cwd": str(work), "hook_event_name": "SessionStart", "source": "startup"}, work)
    try:
        output = json.loads(start.stdout.decode("utf-8"))
        context = output["hookSpecificOutput"]["additionalContext"]
    except Exception as exc:
        context = ""
        info(f"вывод хука: {short(start.stdout.decode('utf-8', 'replace'))} / {short(start.stderr.decode('utf-8', 'replace'))} ({exc})")
    check("SessionStart: код 0 и JSON в UTF-8", start.returncode == 0 and bool(context))
    check("SessionStart: в контексте главная память, пояснение на языке установки",
          "<extended_memory>" in context and T["context_word"] in context, short(context, 300))

    transcript = work / "rollout-live.jsonl"
    transcript.write_bytes((json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": STOP_USER}}, ensure_ascii=False) + "\n").encode("utf-8"))
    payload = {
        "session_id": "live-check", "turn_id": f"live-{int(time.time() * 1000)}", "cwd": str(work),
        "hook_event_name": "Stop", "transcript_path": str(transcript), "last_assistant_message": STOP_ASSISTANT,
    }
    stop = run_hook(hooks["Stop"], payload, work)
    out = stop.stdout.decode("utf-8", "replace").strip()
    check("Stop: код 0 и ответ continue", stop.returncode == 0 and out == '{"continue": true}', short(out + " / " + stop.stderr.decode("utf-8", "replace")))
    auto = sorted((mem_home / "memory" / "auto").glob("*.md"))
    text = "\n".join(read_text(path) for path in auto)
    check("Stop: автозапись создана", bool(auto))
    check("Stop: запрос и итог записаны по-русски без искажений", STOP_USER in text and "данные на месте" in text, short(text, 300))
    check("Stop: имя проекта из рабочей папки", f"`{STOP_PROJECT}`" in text, short(text, 300))
    check("Stop: подписи автозаписи на языке установки", T["auto_label"] in text, short(text, 300))

    found = False
    deadline = time.time() + 90
    while time.time() < deadline and not found:
        code, out, err = run_cli(mem_home, codex_home, "search", STOP_WORD)
        found = code == 0 and "memory/auto/" in out
        if not found:
            time.sleep(2)
    check("Stop: фоновая синхронизация проиндексировала автозапись", found, short(out + err, 300))
    check("Stop: журнал фоновой синхронизации", (mem_home / "logs" / "background-sync.log").is_file())
    runs = json.loads(read_text(mem_home / ".state" / "hook-runs.json"))
    check("оба хука отметились в hook-runs.json", {"SessionStart", "Stop"} <= set(runs), str(runs))


def check_mcp(mem_home: Path, entry: Dict[str, Any]) -> None:
    section("Сервер памяти (MCP) — два независимых клиента")
    check("перед проверкой индекс свободен", wait_no_lock(mem_home))
    first = open_client(entry, "client-a")
    info(f"сервер: {first.server_info}")  # type: ignore[attr-defined]
    listing = first.request("tools/list")["result"]["tools"]
    tools = [tool["name"] for tool in listing]
    check("семь инструментов memory_*", len(tools) == 7 and all(name.startswith("memory_") for name in tools), str(tools))
    described = json.dumps(listing, ensure_ascii=False)
    check("описания инструментов на языке установки", bool(CYRILLIC.search(described)) == (LANG == "ru"), short(described, 300))
    ok, text = first.tool("memory_remember", {"note": MCP_NOTE, "source": "живая проверка"})
    check("клиент A записал факт", ok and T["saved"] in text, short(text))
    first.close()

    second = open_client(entry, "client-b")
    found, text = False, ""
    deadline = time.time() + 60
    while time.time() < deadline and not found:
        ok, text = second.tool("memory_search", {"query": MCP_WORD + " поставщик"})
        found = ok and MCP_WORD in text
        if not found:
            time.sleep(2)
    check("клиент B нашёл записанное клиентом A", found, short(text, 300))
    ok, text = second.tool("memory_status", {})
    check("memory_status отвечает на языке установки", ok and T["status_title"] in text, short(text, 200))
    second.close()


def check_lock(mem_home: Path) -> None:
    section("Замок индекса и проверка «жив ли процесс»")
    check("перед проверкой индекс свободен", wait_no_lock(mem_home))
    os.environ["CODEX_MEMORY_HOME"] = str(mem_home)
    sys.path.insert(0, str(mem_home / "app"))
    import hybrid_memory as hm

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        check("живой процесс определяется как живой", hm._pid_alive(child.pid))
        time.sleep(1)
        check("проверка не завершила сам процесс", child.poll() is None, f"код завершения {child.poll()}")
        hm.LOCK_DIR.mkdir(parents=True)
        (hm.LOCK_DIR / "pid").write_text(f"{child.pid}\n", encoding="utf-8")
        try:
            hm.sync(text_only=True)
            busy = False
        except hm.SyncBusy:
            busy = True
        check("замок живого процесса уважается", busy)
        check("после отказа процесс-владелец всё ещё жив", child.poll() is None)
    finally:
        child.kill()
        child.wait()
    check("завершённый процесс определяется как мёртвый", not hm._pid_alive(child.pid))
    try:
        result = hm.sync(text_only=True)
        check("замок мёртвого процесса снимается, синхронизация проходит", result.get("files", 0) > 0, str(result))
    except Exception as exc:
        check("замок мёртвого процесса снимается, синхронизация проходит", False, repr(exc))
    check("после синхронизации замка нет", not hm.LOCK_DIR.exists())


def schtasks(*args: str) -> Tuple[int, str]:
    proc = subprocess.run(["schtasks", *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    try:
        return proc.returncode, raw.decode("oem")
    except (LookupError, UnicodeDecodeError):
        return proc.returncode, raw.decode("utf-8", errors="replace")


def check_schedule(mem_home: Path) -> None:
    section("Планировщик заданий")
    for key, name, marker in (("sync", "CodexMemorySync", "PT30M"), ("maintain", "CodexMemoryMaintain", "T04:30:00")):
        code, out = schtasks("/Query", "/TN", name, "/XML")
        check(f"задача {name} создана", code == 0, short(out))
        check(f"задача {name}: запускает run_task.py {key}", "run_task.py" in out and f" {key}" in out, short(out, 400))
        check(f"задача {name}: расписание {marker}", marker in out, short(out, 400))
        from_xml = "<StartWhenAvailable>true" in out and "<DisallowStartIfOnBatteries>false" in out
        if not check(f"задача {name}: создана из XML, с настройками питания", from_xml):
            probe = "CodexMemoryProbe"
            code, out = schtasks("/Create", "/TN", probe, "/XML", str(mem_home / ".state" / f"task-{key}.xml"), "/F")
            info(f"повторная регистрация XML: код {code}: {short(out)}")
            schtasks("/Delete", "/TN", probe, "/F")

    root = ET.parse(str(mem_home / ".state" / "task-sync.xml")).getroot()
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) or ""
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) or ""
    workdir = root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns) or ""
    info(f"действие задачи: \"{command}\" {arguments}")
    check("задача запускается через pythonw.exe (без окна)", Path(command).name.lower() == "pythonw.exe", command)
    log = mem_home / "logs" / "sync.log"
    if log.exists():
        log.unlink()
    check("перед запуском действия индекс свободен", wait_no_lock(mem_home))
    proc = subprocess.run(f'"{command}" {arguments}', cwd=workdir, env=agent_env(), timeout=600)
    check("действие задачи отрабатывает с кодом 0", proc.returncode == 0, f"код {proc.returncode}")
    check("действие задачи пишет журнал logs/sync.log", log.is_file())
    if log.is_file() and log.stat().st_size:
        info("sync.log: " + short(log.read_text(encoding="utf-8", errors="replace"), 400))

    code, out = schtasks("/Run", "/TN", "CodexMemorySync")
    info(f"schtasks /Run: код {code}: {short(out, 200)}")
    time.sleep(8)
    code, out = schtasks("/Query", "/TN", "CodexMemorySync", "/V", "/FO", "LIST")
    for line in out.splitlines():
        if any(word in line for word in ("Status", "Last Run Time", "Last Result", "Logon Mode", "Run As User")):
            info(line.strip())


def check_semantic(mem_home: Path, codex_home: Path) -> None:
    section("Поиск по смыслу (Ollama)")
    code, out, err = run_cli(mem_home, codex_home, "remember", SEMANTIC_NOTE)
    check("запись для проверки смысла сохранена", code == 0 and T["saved"] in out, short(out + err))
    status: Dict[str, Any] = {}
    deadline = time.time() + 1200
    while time.time() < deadline:
        code, out, err = run_cli(mem_home, codex_home, "status", "--json")
        try:
            status = json.loads(out)
        except ValueError:
            status = {}
        if status.get("embedding_enabled") and 0 < status.get("chunks", 0) <= status.get("embeddings", 0):
            break
        run_cli(mem_home, codex_home, "sync", "--quiet", timeout=1200)  # досчитываем сами, если фон ещё не успел
    info(f"модель {status.get('embedding_model')}, фрагментов {status.get('chunks')}, векторов {status.get('embeddings')}")
    check("смысловой поиск включён", status.get("embedding_enabled"), short(str(status), 300))
    check("векторы посчитаны для всех фрагментов", 0 < status.get("chunks", 0) <= status.get("embeddings", 0), short(str(status), 300))
    code, out, err = run_cli(mem_home, codex_home, "search", SEMANTIC_QUERY)
    info(short(out, 400))
    check("запрос другими словами находит запись по смыслу", code == 0 and SEMANTIC_MARK in out, short(out + err, 400))


def check_doctor(mem_home: Path, codex_home: Path, embeddings: bool = False) -> None:
    section("Диагностика")
    check("перед диагностикой индекс свободен", wait_no_lock(mem_home))
    code, out, err = run_cli(mem_home, codex_home, "doctor")
    for line in out.splitlines():
        info(line)
    check("memory doctor: всё в порядке", code == 0 and T["doctor_ok"] in out, short(err))
    check("memory doctor: показывает язык", T["doctor_lang"] in out)
    if embeddings:
        check("memory doctor: эмбеддинги работают", T["doctor_emb"] in out)


def check_install_log(path: Path, expect_codex: bool) -> None:
    section("Журнал установки")
    text = path.read_bytes().decode("utf-8", errors="replace")
    check("установка дошла до конца", T["log_done"] in text)
    check("в журнале нет ошибок Python", "Traceback" not in text)
    check("заголовок на языке установки, текст не искажён", T["log_title"] in text)
    if LANG == "en":
        stray = [line.strip() for line in text.splitlines() if CYRILLIC.search(line) and HISTORY_PROJECT not in line]
        check("английская установка: в журнале нет русских сообщений", not stray, short(" | ".join(stray[:5]), 400))
    if expect_codex:
        check("настоящий Кодекс принял config.toml и видит сервер", T["log_codex_ok"] in text)
    elif T["log_no_codex"] in text:
        info("Кодекса на машине нет: config.toml проверен без него")


def check_switch(mem_home: Path, codex_home: Path) -> None:
    section("Смена языка")
    other = "en" if LANG == "ru" else "ru"
    here, there = TEXT[LANG], TEXT[other]
    state = mem_home / ".state" / "lang"
    untouched = {name: (codex_home / name).read_bytes() for name in ("hooks.json", "config.toml")}
    code, out, err = run_cli(mem_home, codex_home, "lang")
    check("memory lang показывает текущий язык", code == 0 and here["lang_line"] in out, short(out + err))
    code, out, err = run_cli(mem_home, codex_home, "lang", other)
    info(short(out + err, 400))
    check(f"memory lang {other}: код 0", code == 0, short(out + err))
    check("выбор сохранён в .state/lang", read_text(state).strip() == other, read_text(state))
    agents = read_text(codex_home / "AGENTS.md")
    check("AGENTS.md: инструкции на новом языке",
          there["agents_title"] in agents and here["agents_title"] not in agents, short(agents, 200))
    check("AGENTS.md: путь к командной строке на месте", str(cli_path(mem_home)) in agents)
    code, out, err = run_cli(mem_home, codex_home, "status")
    check("memory status отвечает на новом языке",
          code == 0 and there["status_files"] in out and here["status_files"] not in out, short(out + err))
    same = all((codex_home / name).read_bytes() == data for name, data in untouched.items())
    check("hooks.json и config.toml при смене языка не тронуты (доверие хукам не слетает)", same)
    code, out, err = run_cli(mem_home, codex_home, "lang", "xx")
    check("memory lang xx: отказ с ненулевым кодом", code != 0, short(out + err))
    code, out, err = run_cli(mem_home, codex_home, "lang", LANG)
    agents = read_text(codex_home / "AGENTS.md")
    check(f"memory lang {LANG}: язык возвращён",
          code == 0 and here["agents_title"] in agents and there["agents_title"] not in agents
          and read_text(state).strip() == LANG, short(out + err))


def installed(args: argparse.Namespace) -> int:
    global LANG, T
    mem_home, codex_home = Path(args.memory_home), Path(args.codex_home)
    section("Язык")
    state = mem_home / ".state" / "lang"
    chosen = read_text(state).strip() if state.is_file() else ""
    check("выбор языка сохранён в .state/lang", chosen in TEXT, repr(chosen))
    if args.lang:
        check(f"установщик выбрал язык {args.lang}", chosen == args.lang, repr(chosen))
    LANG = chosen if chosen in TEXT else (args.lang or "ru")
    T = TEXT[LANG]
    check_layout(mem_home)
    entry = check_codex_config(mem_home, codex_home)
    if args.expect_history:
        check_history(mem_home)
    check_cli(mem_home, codex_home)
    check_hooks(mem_home, codex_home)
    check_mcp(mem_home, entry)
    check_lock(mem_home)
    if args.expect_schedule:
        check_schedule(mem_home)
    if args.expect_embeddings:
        check_semantic(mem_home, codex_home)
    check_doctor(mem_home, codex_home, args.expect_embeddings)
    if args.install_log:
        check_install_log(Path(args.install_log), args.expect_codex)
    check_switch(mem_home, codex_home)
    return 0


# ---------------------------------------------------------------- removed

def removed(args: argparse.Namespace) -> int:
    mem_home, codex_home = Path(args.memory_home), Path(args.codex_home)
    section("После удаления")
    if args.purged:
        check("папка памяти переименована", not mem_home.exists())
        kept = sorted(mem_home.parent.glob(mem_home.name + ".deleted-*"))
        check("данные сохранены под именем .deleted-…", len(kept) == 1 and (kept[0] / "MEMORY.md").is_file(), str(kept))
    else:
        for name in ("app", "bin", ".index"):
            check(f"папка {name} удалена", not (mem_home / name).exists())
        check("MEMORY.md остался", (mem_home / "MEMORY.md").is_file())
        check("дневник остался", any((mem_home / "memory").glob("*.md")))
        check("автозаписи остались", any((mem_home / "memory" / "auto").glob("*.md")))
    config = read_text(codex_home / "config.toml") if (codex_home / "config.toml").exists() else ""
    check("config.toml: записи сервера нет", "extended-memory" not in config, short(config, 300))
    hooks = read_text(codex_home / "hooks.json") if (codex_home / "hooks.json").exists() else ""
    check("hooks.json: хуков памяти нет", "hook_session_start.py" not in hooks and "hook_stop.py" not in hooks)
    agents = read_text(codex_home / "AGENTS.md") if (codex_home / "AGENTS.md").exists() else ""
    check("AGENTS.md: блока памяти нет", "extended-memory:start" not in agents)
    if IS_WINDOWS:
        for name in ("CodexMemorySync", "CodexMemoryMaintain"):
            code, _ = schtasks("/Query", "/TN", name)
            check(f"задача {name} удалена", code != 0)
    return 0


# ---------------------------------------------------------------- запуск

def environment() -> None:
    section("Система")
    info(f"{platform.platform()} · Python {sys.version.split()[0]} · {sys.executable}")
    if IS_WINDOWS:
        import ctypes
        import locale

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        info(f"кодовые страницы: ANSI {kernel32.GetACP()}, OEM {kernel32.GetOEMCP()}, консоль {kernel32.GetConsoleOutputCP()}; "
             f"кодировка Python по умолчанию {locale.getpreferredencoding(False)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Живые проверки расширенной памяти")
    sub = parser.add_subparsers(dest="action")
    sub.required = True
    p = sub.add_parser("prepare")
    p.add_argument("--codex-home", required=True)
    p = sub.add_parser("installed")
    p.add_argument("--memory-home", required=True)
    p.add_argument("--codex-home", required=True)
    p.add_argument("--install-log")
    p.add_argument("--lang", choices=sorted(TEXT))
    p.add_argument("--expect-codex", action="store_true")
    p.add_argument("--expect-schedule", action="store_true")
    p.add_argument("--expect-history", action="store_true")
    p.add_argument("--expect-embeddings", action="store_true")
    p = sub.add_parser("removed")
    p.add_argument("--memory-home", required=True)
    p.add_argument("--codex-home", required=True)
    p.add_argument("--purged", action="store_true")
    args = parser.parse_args()

    if args.action == "prepare":
        return prepare(Path(args.codex_home))
    environment()
    try:
        installed(args) if args.action == "installed" else removed(args)
    except Exception as exc:  # сама проверка не должна падать молча
        import traceback

        traceback.print_exc()
        check("проверки выполнились без исключений", False, repr(exc))
    print("\nИТОГ: " + ("все проверки прошли" if not FAILS else f"не прошло {len(FAILS)}: " + "; ".join(FAILS)), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
