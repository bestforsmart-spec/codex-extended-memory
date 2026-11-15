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
    return dict(os.environ, CODEX_MEMORY_HOME=str(mem_home), PYTHONUTF8="1")


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
    if sys.version_info < (3, 8):
        raise Fail(
            f"Нужен Python 3.8 или новее, а запущен {sys.version.split()[0]}. "
            "Установите свежий с https://www.python.org/downloads/ и запустите install.cmd снова."
        )
    try:
        import sqlite3

        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except Exception as exc:
        raise Fail(
            f"В этом Python нет SQLite FTS5 ({exc}). "
            "Установите Python с https://www.python.org/downloads/ и запустите install.cmd снова."
        )
    ok(f"Python {sys.version.split()[0]} — {sys.executable}")
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
        for name in ("README.md", "uninstall.cmd"):
            if (PKG_DIR / name).is_file():
                shutil.copyfile(PKG_DIR / name, mem_home / name)
                unblock(mem_home / name)
    cli = write_memory_cmd(mem_home, python)
    ok(f"программа установлена в {dst}")
    memory_md = mem_home / "MEMORY.md"
    if memory_md.exists():
        ok("MEMORY.md уже есть — не трогаю")
    else:
        shutil.copyfile(dst / "templates" / "MEMORY.md", memory_md)
        ok("создана главная память MEMORY.md")
    ok(f"командная строка: {cli}")
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
            note("Ollama не найдена")
        else:
            if not ollama_up():
                print("  Запускаю Ollama…", flush=True)
                start_ollama(ollama, mem_home)
                for _ in range(30):
                    if ollama_up():
                        break
                    time.sleep(1)
            if not ollama_up():
                note("Ollama установлена, но не отвечает")
            else:
                if model_ready(model):
                    ok(f"модель {model} уже скачана")
                else:
                    print(f"  Скачиваю модель {model} (около 1,2 ГБ, один раз)…", flush=True)
                    try:
                        if subprocess.call([ollama, "pull", model]) != 0:
                            note(f"не удалось скачать {model}")
                    except OSError as exc:
                        note(f"не удалось скачать {model}: {exc}")
                code, _ = run_memory(python, mem_home, "embeddings", "on", "--model", model)
                enabled = code == 0
    if enabled:
        ok(f"смысловой поиск включён ({model})")
    else:
        run_memory(python, mem_home, "embeddings", "off")
        note("пока работает поиск только по словам. Для поиска по смыслу установите Ollama")
        note("(https://ollama.com/download) и запустите install.cmd ещё раз.")
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
        ("sync", "sync --quiet", SYNC_TRIGGER, "Расширенная память для Кодекса: обновление индекса каждые 30 минут",
         ("/SC", "MINUTE", "/MO", "30")),
        ("maintain", "maintain", MAINTAIN_TRIGGER, "Расширенная память для Кодекса: чистка дублей и секретов в 04:30",
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
            note(f"задачу {name} создать не удалось: {one_line(out)}")
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
    print("Расширенная память для Кодекса")
    print(f"  память:   {mem_home}")
    print(f"  Кодекс:   {codex_home}", flush=True)

    step("1/7 Python")
    python = check_python()

    step("2/7 Файлы памяти")
    cli = install_files(mem_home, python)

    step(f"3/7 Смысловой поиск (Ollama + {args.model})")
    emb_on = setup_embeddings(python, mem_home, args.model, wanted=not args.no_embeddings)

    step("4/7 Подключение к Кодексу")
    if not codex_home.is_dir():
        note(f"папки {codex_home} ещё нет: Кодекс на этом компьютере не запускали. Настройки я создам,")
        note("а если Кодекс у вас работает внутри WSL — ставьте память там же: bash install.sh")
    command = [
        python, str(mem_home / "app" / "configure_codex.py"), "install",
        "--mem-home", str(mem_home), "--codex-home", str(codex_home), "--python", python,
    ]
    if args.no_hooks:
        command.append("--no-hooks")
    if subprocess.call(command, env=child_env(mem_home)) != 0:
        raise Fail("подключить память к Кодексу не удалось — причина в сообщении выше")

    step("5/7 История прошлых сессий Кодекса")
    if args.no_history:
        ok("пропущено (--no-history)")
    else:
        code, out = run_memory(
            python, mem_home, "import-codex-history", "--days", str(args.history_days), "--codex-home", str(codex_home)
        )
        if code == 0:
            ok(one_line(out))
        else:
            note(f"историю загрузить не удалось: {one_line(out)}")

    step("6/7 Первая индексация")
    code, out = run_memory(python, mem_home, "sync", "--text-only", "--quiet")
    if code == 0:
        _, raw = run_memory(python, mem_home, "status", "--json", merge_stderr=False)
        try:
            info = json.loads(raw)
            ok(f"поиск по словам готов: файлов {info['files']}, фрагментов {info['chunks']}")
        except (ValueError, KeyError, TypeError):
            ok("поиск по словам готов")
    else:
        note(f"индексация завершилась с ошибкой ({one_line(out, 200)}) — проверьте: \"{cli}\" doctor")

    step("7/7 Фоновое обновление индекса")
    if args.no_schedule:
        ok("пропущено (--no-schedule)")
    elif not shutil.which("schtasks"):
        note(f"Планировщик заданий недоступен — запускайте иногда вручную: \"{cli}\" sync")
    elif install_schedule(mem_home, python):
        ok("Планировщик заданий: обновление каждые 30 минут, чистка в 04:30")
    if emb_on:
        spawn_sync(mem_home, python)
        ok(f"векторы досчитываются в фоне (прогресс: \"{cli}\" status)")

    print("\nГотово! Что дальше:")
    print("  1. Полностью перезапустите Кодекс (приложение — закрыть и открыть снова; CLI — новая сессия).")
    if not args.no_hooks:
        print("  2. При первом запуске Кодекс спросит про новые хуки (Hooks need review) —")
        print("     выберите «Trust all and continue». Без этого память не подмешивается и не автосохраняется.")
    print(f"  3. Расскажите о себе: «запомни обо мне: …» — или заполните раздел «Обо мне» в {mem_home / 'MEMORY.md'}")
    print(f"  4. Проверка: \"{cli}\" doctor", flush=True)
    return 0


# ---------------------------------------------------------------- удаление

def do_uninstall(args: argparse.Namespace) -> int:
    mem_home = abs_path(args.memory_home or os.environ.get("CODEX_MEMORY_HOME") or str(Path.home() / ".codex-memory"))
    codex_home = abs_path(args.codex_home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    if not mem_home.is_dir():
        print(f"Память не найдена в {mem_home} — нечего удалять.")
        return 0
    if not (mem_home / "app" / "hybrid_memory.py").is_file() and not (mem_home / "MEMORY.md").is_file():
        raise Fail(f"Папка {mem_home} не похожа на расширенную память — ничего не трогаю.")

    print("Отключаю расширенную память от Кодекса…", flush=True)
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
        ok("фоновые задачи Планировщика убраны")

    failed: List[str] = []
    for sub in ("app", "bin", ".index"):
        target = mem_home / sub
        if target.is_dir():
            try:
                shutil.rmtree(target)
            except OSError as exc:
                failed.append(f"{target} ({exc})")
    if failed:
        note("удалилось не всё — закройте Кодекс и запустите ещё раз: " + "; ".join(failed))
    else:
        ok("программа и индекс удалены")

    if args.purge:
        target = mem_home.with_name(f"{mem_home.name}.deleted-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.rename(mem_home, target)
            note(f"данные переименованы в {target} — удалите папку сами")
        except OSError as exc:
            note(f"переименовать {mem_home} не удалось ({exc}) — удалите папку сами")
    else:
        ok(f"заметки и главная память остались в {mem_home} (MEMORY.md, memory, backups)")
        print("    Вернуть всё: запустите install.cmd снова. Удалить и данные: uninstall.cmd --purge")
    print("Перезапустите Кодекс.", flush=True)
    return 0


# ---------------------------------------------------------------- запуск

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Расширенная память для Кодекса: установка и удаление на Windows")
    sub = parser.add_subparsers(dest="action", metavar="действие")
    sub.required = True

    inst = sub.add_parser("install", prog="install.cmd", description="Установка расширенной памяти для Кодекса на Windows")
    inst.add_argument("--no-embeddings", action="store_true", help="не включать смысловой поиск (без Ollama, только поиск по словам)")
    inst.add_argument("--model", default="bge-m3", metavar="ИМЯ", help="модель эмбеддингов Ollama (по умолчанию bge-m3)")
    inst.add_argument("--no-history", action="store_true", help="не загружать в память прошлые сессии Кодекса")
    inst.add_argument("--history-days", type=int, default=120, metavar="N", help="за сколько дней загружать историю (по умолчанию 120)")
    inst.add_argument("--no-hooks", action="store_true", help="без хуков (автоподмешивания памяти и автозаписи)")
    inst.add_argument("--no-schedule", action="store_true", help="без фонового обновления индекса")

    unin = sub.add_parser("uninstall", prog="uninstall.cmd", description="Отключение расширенной памяти от Кодекса на Windows")
    unin.add_argument("--purge", action="store_true", help="убрать и сами данные памяти (папка переименовывается, удалить её нужно самому)")

    for p in (inst, unin):
        p.add_argument("--memory-home", metavar="ПАПКА", help=r"где хранится память (по умолчанию %%USERPROFILE%%\.codex-memory)")
        p.add_argument("--codex-home", metavar="ПАПКА", help=r"папка настроек Кодекса (по умолчанию %%USERPROFILE%%\.codex)")
        p.add_argument("--no-pause", action="store_true", help="не ждать нажатия Enter в конце")
    return parser


def pause(skip: bool) -> None:
    """Окно, открытое двойным щелчком, закрывается сразу — даём прочитать итог."""
    if skip or not IS_WINDOWS:
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажмите Enter, чтобы закрыть окно…")
    except (EOFError, OSError):
        pass


def main(argv: Optional[List[str]] = None) -> int:
    fix_stdio()
    args = build_parser().parse_args(argv)
    if not IS_WINDOWS and not os.environ.get("CODEX_MEMORY_SETUP_ANY_OS"):
        print("Этот установщик — для Windows. На macOS и Linux запустите: bash install.sh")
        return 1
    try:
        code = do_install(args) if args.action == "install" else do_uninstall(args)
    except Fail as exc:
        print(f"  ✗ {exc}", flush=True)
        code = 1
    except KeyboardInterrupt:
        print("\nПрервано.", flush=True)
        code = 130
    pause(args.no_pause)
    return code


if __name__ == "__main__":
    sys.exit(main())
