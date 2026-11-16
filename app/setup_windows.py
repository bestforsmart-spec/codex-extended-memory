#!/usr/bin/env python3
r"""Установка и удаление расширенной памяти для Кодекса на Windows.

Запускается из install.cmd и uninstall.cmd и делает то же, что install.sh на macOS и Linux:
копирует программу в %USERPROFILE%\.codex-memory, подключает её к Кодексу, загружает историю
сессий, строит индекс и ставит фоновые задачи в Планировщик заданий. Повторный запуск безопасен:
обновляет программу, данные памяти не трогает. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n  # noqa: E402
from i18n import L  # noqa: E402

IS_WINDOWS = os.name == "nt"
PKG_DIR = Path(__file__).resolve().parents[1]
OLLAMA_URL = "http://127.0.0.1:11434"
TASKS = {"sync": "CodexMemorySync", "maintain": "CodexMemoryMaintain"}

SYNC_TRIGGER = (
    "    <TimeTrigger>\n"
    "      <Repetition>\n"
    "        <Interval>PT30M</Interval>\n"
    "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
    "      </Repetition>\n"
    "      <StartBoundary>2026-01-01T00:00:00</StartBoundary>\n"
    "      <Enabled>true</Enabled>\n"
    "    </TimeTrigger>\n"
)
MAINTAIN_TRIGGER = (
    "    <CalendarTrigger>\n"
    "      <StartBoundary>2026-01-01T04:30:00</StartBoundary>\n"
    "      <Enabled>true</Enabled>\n"
    "      <ScheduleByDay>\n"
    "        <DaysInterval>1</DaysInterval>\n"
    "      </ScheduleByDay>\n"
    "    </CalendarTrigger>\n"
)


class Fail(Exception):
    """Установку нельзя продолжать; текст — что сделать пользователю."""


def fix_stdio() -> None:
    """Вывод в трубу или файл на Windows идёт в cp1251 и падает на «✓» — переводим в UTF-8."""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def step(text: str) -> None:
    print(f"\n{text}", flush=True)


def ok(text: str) -> None:
    print(f"  ✓ {text}", flush=True)


def note(text: str) -> None:
    print(f"  ! {text}", flush=True)


def one_line(text: str, limit: int = 300) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def abs_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def child_env(mem_home: Path) -> Dict[str, str]:
    # Язык передаём явно: команды памяти, которые зовёт установщик, отвечают на его языке.
    return dict(os.environ, CODEX_MEMORY_HOME=str(mem_home), PYTHONUTF8="1", **{i18n.ENV_VAR: i18n.current()})


def detached() -> Dict[str, Any]:
    """Параметры Popen для процесса, который переживёт установщик и не откроет окно консоли."""
    if IS_WINDOWS:
        return {"creationflags": 0x08000000 | 0x00000200, "close_fds": True}  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    return {"start_new_session": True, "close_fds": True}


def run_memory(python: str, mem_home: Path, *args: str, merge_stderr: bool = True) -> Tuple[int, str]:
    """Запускает командную строку памяти и возвращает (код, вывод)."""
    proc = subprocess.run(
        [python, str(mem_home / "app" / "hybrid_memory.py"), *args],
        env=child_env(mem_home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "").strip()


# ---------------------------------------------------------------- 1. Python

def check_python() -> str:
    version = sys.version.split()[0]
    if sys.version_info < (3, 8):
        raise Fail(L(
            f"Нужен Python 3.8 или новее, а запущен {version}. "
            "Установите свежий с https://www.python.org/downloads/ и запустите install.cmd снова.",
            f"Python 3.8 or newer is required, but this is {version}. "
            "Install a current one from https://www.python.org/downloads/ and run install.cmd again.",
        ))
    try:
        import sqlite3

        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except Exception as exc:
        raise Fail(L(
            f"В этом Python нет SQLite FTS5 ({exc}). "
            "Установите Python с https://www.python.org/downloads/ и запустите install.cmd снова.",
            f"This Python has no SQLite FTS5 ({exc}). "
            "Install Python from https://www.python.org/downloads/ and run install.cmd again.",
        ))
    ok(f"Python {version} — {sys.executable}")
    return sys.executable


# ---------------------------------------------------------------- 2. Файлы

def cmd_literal(path: str) -> str:
    """Путь для .cmd-файла. cmd читает такие файлы в кодировке консоли, поэтому русское имя
    пользователя в пути по возможности прячем за переменной среды — файл остаётся в ASCII."""
    text = path.replace("%", "%%")
    if text.isascii():
        return text
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramFiles", "ProgramW6432"):
        base = (os.environ.get(var) or "").rstrip("\\/")
        if base and path.lower().startswith(base.lower() + "\\"):
            candidate = f"%{var}%" + path[len(base):].replace("%", "%%")
            if candidate.isascii():
                return candidate
    return text


def memory_cmd_text(python: str) -> str:
    lines = [
        "@echo off",
        "rem Extended memory for Codex: command line. Usage: memory --help",
        "setlocal",
        'set "CODEX_MEMORY_HOME=%~dp0.."',
        f'"{cmd_literal(python)}" "%~dp0..\\app\\hybrid_memory.py" %*',
        "exit /b %ERRORLEVEL%",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_memory_cmd(mem_home: Path, python: str) -> Path:
    text = memory_cmd_text(python)
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError:
        try:
            data = text.encode("oem" if IS_WINDOWS else "utf-8")
        except (UnicodeEncodeError, LookupError):
            data = text.encode("utf-8")
    path = mem_home / "bin" / "memory.cmd"
    path.write_bytes(data)
    return path


def unblock(path: Path) -> None:
    """Снимает с файла метку «скачан из интернета», чтобы Windows не переспрашивала при запуске."""
    if IS_WINDOWS:
        try:
            os.remove(f"{path}:Zone.Identifier")
        except OSError:
            pass


def install_files(mem_home: Path, python: str) -> Path:
    for sub in ("app", "bin", "memory", "logs", "backups", ".state"):
        (mem_home / sub).mkdir(parents=True, exist_ok=True)
    src, dst = PKG_DIR / "app", mem_home / "app"
    if src.resolve() != dst.resolve():
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("README.md", "README.ru.md", "uninstall.cmd"):
            if (PKG_DIR / name).is_file():
                shutil.copyfile(PKG_DIR / name, mem_home / name)
                unblock(mem_home / name)
    cli = write_memory_cmd(mem_home, python)
    i18n.save(i18n.current(), mem_home)
    ok(L(f"программа установлена в {dst}", f"program installed to {dst}"))
    memory_md = mem_home / "MEMORY.md"
    if memory_md.exists():
        ok(L("MEMORY.md уже есть — не трогаю", "MEMORY.md already exists — leaving it as is"))
    else:
        shutil.copyfile(dst / "templates" / i18n.current() / "MEMORY.md", memory_md)
        ok(L("создана главная память MEMORY.md", "created the main memory file MEMORY.md"))
    ok(L(f"командная строка: {cli}", f"command line: {cli}"))
    return cli


# ---------------------------------------------------------------- 3. Ollama

def http_json(url: str, timeout: float) -> Optional[Dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return None


def ollama_up() -> bool:
    return http_json(f"{OLLAMA_URL}/api/version", 2) is not None


def find_ollama() -> Optional[str]:
    candidates = [shutil.which("ollama") or ""]
    for var, tail in (("LOCALAPPDATA", ("Programs", "Ollama", "ollama.exe")), ("ProgramFiles", ("Ollama", "ollama.exe"))):
        base = os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, *tail))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def start_ollama(ollama: str, mem_home: Path) -> None:
    try:
        tray = Path(ollama).with_name("ollama app.exe")
        if IS_WINDOWS and tray.is_file():
            os.startfile(str(tray))  # type: ignore[attr-defined]
            return
        with open(mem_home / "logs" / "ollama.log", "a", encoding="utf-8") as log:
            subprocess.Popen([ollama, "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, **detached())
    except OSError:
        pass


def model_ready(model: str) -> bool:
    tags = http_json(f"{OLLAMA_URL}/api/tags", 5) or {}
    names = [str(item.get("name", "")) for item in tags.get("models", []) if isinstance(item, dict)]
    return any(name == model or name.startswith(model + ":") for name in names)


def setup_embeddings(python: str, mem_home: Path, model: str, wanted: bool) -> bool:
    enabled = False
    if wanted:
        ollama = find_ollama()
        if not ollama:
            note(L("Ollama не найдена", "Ollama not found"))
        else:
            if not ollama_up():
                print(L("  Запускаю Ollama…", "  Starting Ollama…"), flush=True)
                start_ollama(ollama, mem_home)
                for _ in range(30):
                    if ollama_up():
                        break
                    time.sleep(1)
            if not ollama_up():
                note(L("Ollama установлена, но не отвечает", "Ollama is installed but does not respond"))
            else:
                if model_ready(model):
                    ok(L(f"модель {model} уже скачана", f"the {model} model is already downloaded"))
                else:
                    print(L(
                        f"  Скачиваю модель {model} (около 1,2 ГБ, один раз)…",
                        f"  Downloading the {model} model (about 1.2 GB, once)…",
                    ), flush=True)
                    try:
                        if subprocess.call([ollama, "pull", model]) != 0:
                            note(L(f"не удалось скачать {model}", f"could not download {model}"))
                    except OSError as exc:
                        note(L(f"не удалось скачать {model}: {exc}", f"could not download {model}: {exc}"))
                code, _ = run_memory(python, mem_home, "embeddings", "on", "--model", model)
                enabled = code == 0
    if enabled:
        ok(L(f"смысловой поиск включён ({model})", f"semantic search is on ({model})"))
    else:
        run_memory(python, mem_home, "embeddings", "off")
        note(L(
            "пока работает поиск только по словам. Для поиска по смыслу установите Ollama",
            "for now only keyword search works. For semantic search, install Ollama",
        ))
        note(L(
            "(https://ollama.com/download) и запустите install.cmd ещё раз.",
            "(https://ollama.com/download) and run install.cmd again.",
        ))
    return enabled


# ---------------------------------------------------------------- 7. Планировщик заданий

def pythonw_for(python: str) -> str:
    """pythonw.exe запускает задачу без окна консоли; если его нет — обычный python."""
    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else python


def current_user() -> Optional[str]:
    name, domain = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    if name and domain:
        return f"{domain}\\{name}"
    return name or None


def task_xml(command: str, arguments: str, workdir: str, trigger: str, description: str, user: Optional[str]) -> str:
    user_line = f"      <UserId>{xml_escape(user)}</UserId>\n" if user else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>{xml_escape(description)}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        f"{trigger}"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"{user_line}"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{xml_escape(command)}</Command>\n"
        f"      <Arguments>{xml_escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{xml_escape(workdir)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def decode_console(data: bytes) -> str:
    for encoding in (("oem",) if IS_WINDOWS else ()) + ("utf-8",):
        try:
            return data.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace").strip()


def schtasks(*args: str) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["schtasks", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, decode_console(proc.stdout or b"")


def install_schedule(mem_home: Path, python: str) -> bool:
    """Две задачи Планировщика: индекс каждые 30 минут и чистка в 04:30. Права администратора не нужны."""
    runner = pythonw_for(python)
    script = str(mem_home / "app" / "run_task.py")
    user = current_user()
    plans = (
        ("sync", "sync --quiet", SYNC_TRIGGER,
         L("Расширенная память для Кодекса: обновление индекса каждые 30 минут",
           "Extended Memory for Codex: index update every 30 minutes"),
         ("/SC", "MINUTE", "/MO", "30")),
        ("maintain", "maintain", MAINTAIN_TRIGGER,
         L("Расширенная память для Кодекса: чистка дублей и секретов в 04:30",
           "Extended Memory for Codex: cleanup of duplicates and secrets at 04:30"),
         ("/SC", "DAILY", "/ST", "04:30")),
    )
    done = True
    for key, task_args, trigger, description, simple in plans:
        name = TASKS[key]
        arguments = f'"{script}" {task_args}'
        xml_path = mem_home / ".state" / f"task-{key}.xml"
        xml_path.write_text(task_xml(runner, arguments, str(mem_home), trigger, description, user), encoding="utf-16")
        code, out = schtasks("/Create", "/TN", name, "/XML", str(xml_path), "/F")
        if code != 0:  # запасной путь: простая задача без тонких настроек
            code, out = schtasks("/Create", "/TN", name, *simple, "/TR", f'"{runner}" {arguments}', "/F")
        if code != 0:
            done = False
            note(L(f"задачу {name} создать не удалось: {one_line(out)}", f"could not create the {name} task: {one_line(out)}"))
    return done


def remove_schedule() -> None:
    for name in TASKS.values():
        schtasks("/Delete", "/TN", name, "/F")


def spawn_sync(mem_home: Path, python: str) -> None:
    """Досчитывает векторы в фоне, не дожидаясь первого запуска по расписанию."""
    runner = pythonw_for(python) if IS_WINDOWS else python
    try:
        subprocess.Popen(
            [runner, str(mem_home / "app" / "run_task.py"), "sync", "--quiet"],
            env=child_env(mem_home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detached(),
        )
    except OSError:
        pass


# ---------------------------------------------------------------- установка

def do_install(args: argparse.Namespace) -> int:
    mem_home = abs_path(args.memory_home or os.environ.get("CODEX_MEMORY_HOME") or str(Path.home() / ".codex-memory"))
    codex_home = abs_path(args.codex_home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    print(L("Расширенная память для Кодекса", "Extended Memory for Codex"))
    print(L(f"  память:   {mem_home}", f"  memory:   {mem_home}"))
    print(L(f"  Кодекс:   {codex_home}", f"  Codex:    {codex_home}"))
    print(L(
        "  язык:     русский (другой: --lang en, после установки — memory lang en)",
        "  language: English (to change: --lang ru, after installing: memory lang ru)",
    ), flush=True)

    step("1/7 Python")
    python = check_python()

    step(L("2/7 Файлы памяти", "2/7 Memory files"))
    cli = install_files(mem_home, python)

    step(L(f"3/7 Смысловой поиск (Ollama + {args.model})", f"3/7 Semantic search (Ollama + {args.model})"))
    emb_on = setup_embeddings(python, mem_home, args.model, wanted=not args.no_embeddings)

    step(L("4/7 Подключение к Кодексу", "4/7 Connecting to Codex"))
    if not codex_home.is_dir():
        note(L(
            f"папки {codex_home} ещё нет: Кодекс на этом компьютере не запускали. Настройки я создам,",
            f"the {codex_home} folder does not exist yet: Codex has not been run on this computer. I will create the settings;",
        ))
        note(L(
            "а если Кодекс у вас работает внутри WSL — ставьте память там же: bash install.sh",
            "if your Codex runs inside WSL, install the memory there instead: bash install.sh",
        ))
    command = [
        python, str(mem_home / "app" / "configure_codex.py"), "install",
        "--mem-home", str(mem_home), "--codex-home", str(codex_home), "--python", python,
        "--lang", i18n.current(),
    ]
    if args.no_hooks:
        command.append("--no-hooks")
    if subprocess.call(command, env=child_env(mem_home)) != 0:
        raise Fail(L(
            "подключить память к Кодексу не удалось — причина в сообщении выше",
            "could not connect the memory to Codex — the reason is in the message above",
        ))

    step(L("5/7 История прошлых сессий Кодекса", "5/7 History of past Codex sessions"))
    if args.no_history:
        ok(L("пропущено (--no-history)", "skipped (--no-history)"))
    else:
        code, out = run_memory(
            python, mem_home, "import-codex-history", "--days", str(args.history_days), "--codex-home", str(codex_home)
        )
        if code == 0:
            ok(one_line(out))
        else:
            note(L(f"историю загрузить не удалось: {one_line(out)}", f"could not load the history: {one_line(out)}"))

    step(L("6/7 Первая индексация", "6/7 First indexing"))
    code, out = run_memory(python, mem_home, "sync", "--text-only", "--quiet")
    if code == 0:
        _, raw = run_memory(python, mem_home, "status", "--json", merge_stderr=False)
        try:
            info = json.loads(raw)
            ok(L(
                f"поиск по словам готов: файлов {info['files']}, фрагментов {info['chunks']}",
                f"keyword search is ready: files {info['files']}, fragments {info['chunks']}",
            ))
        except (ValueError, KeyError, TypeError):
            ok(L("поиск по словам готов", "keyword search is ready"))
    else:
        note(L(
            f"индексация завершилась с ошибкой ({one_line(out, 200)}) — проверьте: \"{cli}\" doctor",
            f"indexing ended with an error ({one_line(out, 200)}) — check with: \"{cli}\" doctor",
        ))

    step(L("7/7 Фоновое обновление индекса", "7/7 Background index updates"))
    if args.no_schedule:
        ok(L("пропущено (--no-schedule)", "skipped (--no-schedule)"))
    elif not shutil.which("schtasks"):
        note(L(
            f"Планировщик заданий недоступен — запускайте иногда вручную: \"{cli}\" sync",
            f"Task Scheduler is unavailable — run this by hand from time to time: \"{cli}\" sync",
        ))
    elif install_schedule(mem_home, python):
        ok(L(
            "Планировщик заданий: обновление каждые 30 минут, чистка в 04:30",
            "Task Scheduler: index update every 30 minutes, cleanup at 04:30",
        ))
    if emb_on:
        spawn_sync(mem_home, python)
        ok(L(
            f"векторы досчитываются в фоне (прогресс: \"{cli}\" status)",
            f"vectors are being computed in the background (progress: \"{cli}\" status)",
        ))

    print(L("\nГотово! Что дальше:", "\nDone! What next:"))
    print(L(
        "  1. Полностью перезапустите Кодекс (приложение — закрыть и открыть снова; CLI — новая сессия).",
        "  1. Restart Codex completely (the app: quit and open it again; the CLI: start a new session).",
    ))
    if not args.no_hooks:
        print(L(
            "  2. При первом запуске Кодекс спросит про новые хуки (Hooks need review) —",
            "  2. On first launch Codex will ask about the new hooks (Hooks need review):",
        ))
        print(L(
            "     выберите «Trust all and continue». Без этого память не подмешивается и не автосохраняется.",
            "     choose \"Trust all and continue\". Without it the memory is neither added to sessions nor saved automatically.",
        ))
    print(L(
        f"  3. Расскажите о себе: «запомни обо мне: …» — или заполните раздел «Обо мне» в {mem_home / 'MEMORY.md'}",
        f"  3. Tell it about yourself: \"remember about me: …\" — or fill in the \"About me\" section of {mem_home / 'MEMORY.md'}",
    ))
    print(L(f"  4. Проверка: \"{cli}\" doctor", f"  4. Check: \"{cli}\" doctor"), flush=True)
    return 0


# ---------------------------------------------------------------- удаление

def do_uninstall(args: argparse.Namespace) -> int:
    mem_home = abs_path(args.memory_home or os.environ.get("CODEX_MEMORY_HOME") or str(Path.home() / ".codex-memory"))
    codex_home = abs_path(args.codex_home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    if not mem_home.is_dir():
        print(L(f"Память не найдена в {mem_home} — нечего удалять.", f"No memory found in {mem_home} — nothing to remove."))
        return 0
    if not (mem_home / "app" / "hybrid_memory.py").is_file() and not (mem_home / "MEMORY.md").is_file():
        raise Fail(L(
            f"Папка {mem_home} не похожа на расширенную память — ничего не трогаю.",
            f"The {mem_home} folder does not look like extended memory — leaving it untouched.",
        ))

    print(L("Отключаю расширенную память от Кодекса…", "Disconnecting extended memory from Codex…"), flush=True)
    configure = mem_home / "app" / "configure_codex.py"
    if not configure.is_file():
        configure = PKG_DIR / "app" / "configure_codex.py"
    if configure.is_file():
        subprocess.call(
            [sys.executable, str(configure), "uninstall", "--mem-home", str(mem_home), "--codex-home", str(codex_home)],
            env=child_env(mem_home),
        )
    if shutil.which("schtasks"):
        remove_schedule()
        ok(L("фоновые задачи Планировщика убраны", "Task Scheduler background tasks removed"))

    failed: List[str] = []
    for sub in ("app", "bin", ".index"):
        target = mem_home / sub
        if target.is_dir():
            try:
                shutil.rmtree(target)
            except OSError as exc:
                failed.append(f"{target} ({exc})")
    if failed:
        note(L(
            "удалилось не всё — закройте Кодекс и запустите ещё раз: ",
            "not everything was removed — close Codex and run this again: ",
        ) + "; ".join(failed))
    else:
        ok(L("программа и индекс удалены", "program and index removed"))

    if args.purge:
        target = mem_home.with_name(f"{mem_home.name}.deleted-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.rename(mem_home, target)
            note(L(f"данные переименованы в {target} — удалите папку сами", f"the data folder was renamed to {target} — delete it yourself"))
        except OSError as exc:
            note(L(
                f"переименовать {mem_home} не удалось ({exc}) — удалите папку сами",
                f"could not rename {mem_home} ({exc}) — delete the folder yourself",
            ))
    else:
        ok(L(
            f"заметки и главная память остались в {mem_home} (MEMORY.md, memory, backups)",
            f"your notes and the main memory stay in {mem_home} (MEMORY.md, memory, backups)",
        ))
        print(L(
            "    Вернуть всё: запустите install.cmd снова. Удалить и данные: uninstall.cmd --purge",
            "    To bring it back, run install.cmd again. To delete the data too: uninstall.cmd --purge",
        ))
    print(L("Перезапустите Кодекс.", "Restart Codex."), flush=True)
    return 0


# ---------------------------------------------------------------- запуск

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=L(
        "Расширенная память для Кодекса: установка и удаление на Windows",
        "Extended Memory for Codex: install and uninstall on Windows",
    ))
    sub = parser.add_subparsers(dest="action", metavar=L("действие", "action"))
    sub.required = True
    name, folder = L("ИМЯ", "NAME"), L("ПАПКА", "DIR")

    inst = sub.add_parser("install", prog="install.cmd", description=L(
        "Установка расширенной памяти для Кодекса на Windows", "Installs Extended Memory for Codex on Windows",
    ))
    inst.add_argument("--no-embeddings", action="store_true", help=L(
        "не включать смысловой поиск (без Ollama, только поиск по словам)",
        "do not turn on semantic search (no Ollama, keyword search only)",
    ))
    inst.add_argument("--model", default="bge-m3", metavar=name, help=L(
        "модель эмбеддингов Ollama (по умолчанию bge-m3)", "Ollama embedding model (default: bge-m3)",
    ))
    inst.add_argument("--no-history", action="store_true", help=L(
        "не загружать в память прошлые сессии Кодекса", "do not load past Codex sessions into the memory",
    ))
    inst.add_argument("--history-days", type=int, default=120, metavar="N", help=L(
        "за сколько дней загружать историю (по умолчанию 120)", "how many days of history to load (default: 120)",
    ))
    inst.add_argument("--no-hooks", action="store_true", help=L(
        "без хуков (автоподмешивания памяти и автозаписи)", "no hooks (no automatic memory injection, no auto-records)",
    ))
    inst.add_argument("--no-schedule", action="store_true", help=L(
        "без фонового обновления индекса", "no background index updates",
    ))

    unin = sub.add_parser("uninstall", prog="uninstall.cmd", description=L(
        "Отключение расширенной памяти от Кодекса на Windows", "Disconnects Extended Memory from Codex on Windows",
    ))
    unin.add_argument("--purge", action="store_true", help=L(
        "убрать и сами данные памяти (папка переименовывается, удалить её нужно самому)",
        "remove the memory data as well (the folder is renamed; delete it yourself)",
    ))

    for p in (inst, unin):
        p.add_argument("--memory-home", metavar=folder, help=L(
            r"где хранится память (по умолчанию %%USERPROFILE%%\.codex-memory)",
            r"where the memory is stored (default: %%USERPROFILE%%\.codex-memory)",
        ))
        p.add_argument("--codex-home", metavar=folder, help=L(
            r"папка настроек Кодекса (по умолчанию %%USERPROFILE%%\.codex)",
            r"Codex settings folder (default: %%USERPROFILE%%\.codex)",
        ))
        p.add_argument("--lang", choices=list(i18n.SUPPORTED), help=L(
            "язык сообщений и инструкций для агента: ru или en (по умолчанию — по языку системы)",
            "language of the messages and the agent instructions: ru or en (default: the system language)",
        ))
        p.add_argument("--no-pause", action="store_true", help=L(
            "не ждать нажатия Enter в конце", "do not wait for Enter at the end",
        ))
    return parser


def _argument(argv: List[str], name: str) -> Optional[str]:
    """Значение параметра «--name X» или «--name=X» до разбора argparse: язык нужен уже для текста справки."""
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def choose_language(argv: List[str]) -> str:
    """Флаг --lang → CODEX_MEMORY_LANG → сохранённый выбор → русский для памяти, поставленной до 1.2.0 → язык системы."""
    home = abs_path(_argument(argv, "--memory-home") or os.environ.get("CODEX_MEMORY_HOME") or str(Path.home() / ".codex-memory"))
    legacy = (home / "MEMORY.md").is_file() or (home / "app" / "hybrid_memory.py").is_file()
    return i18n.pick_for_install(i18n.normalize(_argument(argv, "--lang")), home, legacy)


def pause(skip: bool) -> None:
    """Окно, открытое двойным щелчком, закрывается сразу — даём прочитать итог."""
    if skip or not IS_WINDOWS:
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input(L("\nНажмите Enter, чтобы закрыть окно…", "\nPress Enter to close the window…"))
    except (EOFError, OSError):
        pass


def main(argv: Optional[List[str]] = None) -> int:
    fix_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    i18n.use(choose_language(argv))
    args = build_parser().parse_args(argv)
    if not IS_WINDOWS and not os.environ.get("CODEX_MEMORY_SETUP_ANY_OS"):
        print(L(
            "Этот установщик — для Windows. На macOS и Linux запустите: bash install.sh",
            "This installer is for Windows. On macOS and Linux run: bash install.sh",
        ))
        return 1
    try:
        code = do_install(args) if args.action == "install" else do_uninstall(args)
    except Fail as exc:
        print(f"  ✗ {exc}", flush=True)
        code = 1
    except KeyboardInterrupt:
        print(L("\nПрервано.", "\nInterrupted."), flush=True)
        code = 130
    pause(args.no_pause)
    return code


if __name__ == "__main__":
    sys.exit(main())
