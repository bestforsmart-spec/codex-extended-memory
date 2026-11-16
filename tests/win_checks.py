"""Проверки поддержки Windows, которые можно выполнить на Маке (без самой Windows).

usage: win_checks.py <корень репозитория> <временная папка>

Работает на любой системе; нужен Python 3.11+ (tomllib).
"""
import ast
import json
import os
import pathlib
import sys
import tomllib
import xml.etree.ElementTree as ET

for stream in (sys.stdout, sys.stderr):  # на Windows вывод в трубу идёт не в UTF-8
    stream.reconfigure(encoding="utf-8", errors="replace")

pkg = pathlib.Path(sys.argv[1]).resolve()
tmp = pathlib.Path(sys.argv[2])
app = pkg / "app"
os.environ["CODEX_MEMORY_HOME"] = str(tmp / "mem")  # чтобы импорт движка не смотрел в настоящую память
sys.path.insert(0, str(app))

fails = []


def check(name, cond, detail=""):
    print("ok  " if cond else "FAIL", name, "" if cond else f"— {detail}")
    if not cond:
        fails.append(name)


# 1. Синтаксис совместим с Python 3.8
for path in sorted(app.glob("*.py")):
    try:
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))
        check(f"синтаксис 3.8: {path.name}", True)
    except SyntaxError as exc:
        check(f"синтаксис 3.8: {path.name}", False, str(exc))

# 2. Файлы .cmd: только ASCII, переводы строк Windows, без блоков в скобках
for name in ("install.cmd", "uninstall.cmd"):
    data = (pkg / name).read_bytes()
    check(f"{name}: ASCII", all(b < 128 for b in data))
    check(f"{name}: CRLF", b"\n" not in data.replace(b"\r\n", b""))
    lines = data.decode("ascii").splitlines()
    check(f"{name}: без блоков в скобках", not any(line.rstrip().endswith("(") or line.strip() == ")" for line in lines))
    labels = {line.strip() for line in lines if line.startswith(":")}
    gotos = {word for line in lines for word in line.split() if word.startswith(":") and "goto" in line}
    check(f"{name}: все метки goto существуют", gotos <= labels, f"{gotos - labels}")

# 3. Настройки Кодекса с путями Windows
import configure_codex as cc  # noqa: E402

PY = r"C:\Users\Иван\AppData\Local\Programs\Python\Python312\python.exe"
MEM = pathlib.PureWindowsPath(r"C:\Users\Иван\.codex-memory")
win = cc.hook_command(PY, str(MEM / "app" / "hook_stop.py"), windows=True)
check("хук Windows: двойные кавычки", win == f'"{PY}" "{MEM}\\app\\hook_stop.py"', win)
posix = cc.hook_command("/usr/bin/python3", "/Users/x/.codex-memory/app/hook_stop.py", windows=False)
check("хук macOS/Linux: как раньше", posix == "/usr/bin/python3 /Users/x/.codex-memory/app/hook_stop.py", posix)
spaced = cc.hook_command("/opt/my py/python3", "/Users/x y/app/hook_stop.py", windows=False)
check("хук macOS/Linux: пробелы", spaced == "'/opt/my py/python3' '/Users/x y/app/hook_stop.py'", spaced)
check("хук Windows узнаётся при обновлении", cc._is_ours({"command": win}, MEM / "app"))
check("хук в JSON читается обратно", json.loads(json.dumps({"command": win}, ensure_ascii=False))["command"] == win)

block = cc.server_block(PY, MEM / "app", MEM)
server = tomllib.loads(block)["mcp_servers"]["extended-memory"]
check("TOML: command", server["command"] == PY, server["command"])
check("TOML: args", server["args"] == [str(MEM / "app" / "mcp_server.py")], str(server["args"]))
check("TOML: env", server["env"]["CODEX_MEMORY_HOME"] == str(MEM), str(server["env"]))

# 4. Установщик Windows: задачи Планировщика и memory.cmd
import setup_windows as sw  # noqa: E402

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
ARGS = f'"{MEM}\\app\\run_task.py" sync --quiet'
for name, trigger in (("sync", sw.SYNC_TRIGGER), ("maintain", sw.MAINTAIN_TRIGGER)):
    xml = sw.task_xml(PY.replace("python.exe", "pythonw.exe"), ARGS, str(MEM), trigger, "Память & <тест>", "PC\\Иван")
    try:
        root = ET.fromstring(xml.encode("utf-16"))
    except ET.ParseError as exc:
        check(f"XML {name}: разбирается", False, str(exc))
        continue
    check(f"XML {name}: разбирается", root.tag == "{%s}Task" % NS["t"])
    check(f"XML {name}: команда", root.find("t:Actions/t:Exec/t:Command", NS).text.endswith("pythonw.exe"))
    check(f"XML {name}: аргументы", root.find("t:Actions/t:Exec/t:Arguments", NS).text == ARGS)
    check(f"XML {name}: описание", root.find("t:RegistrationInfo/t:Description", NS).text == "Память & <тест>")
    check(f"XML {name}: пользователь", root.find("t:Principals/t:Principal/t:UserId", NS).text == "PC\\Иван")
    check(f"XML {name}: работает от батареи", root.find("t:Settings/t:DisallowStartIfOnBatteries", NS).text == "false")
    check(f"XML {name}: один триггер", len(list(root.find("t:Triggers", NS))) == 1)
no_user = ET.fromstring(sw.task_xml("p", "a", "w", sw.SYNC_TRIGGER, "d", None).encode("utf-16"))
check("XML без пользователя", no_user.find("t:Principals/t:Principal/t:UserId", NS) is None)

os.environ["LOCALAPPDATA"] = r"C:\Users\Иван\AppData\Local"
os.environ["USERPROFILE"] = r"C:\Users\Иван"
literal = sw.cmd_literal(PY)
check("cmd: русское имя спрятано за переменной", literal == r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe", literal)
plain = r"C:\Program Files\Python312\python.exe"
check("cmd: путь в ASCII не меняется", sw.cmd_literal(plain) == plain)
check("cmd: процент удваивается", sw.cmd_literal(r"C:\100%\python.exe") == r"C:\100%%\python.exe")
text = sw.memory_cmd_text(PY)
check("memory.cmd: ASCII и CRLF", text.isascii() and "\n" not in text.replace("\r\n", ""), text)
check("memory.cmd: путь к движку от самого файла", '"%~dp0..\\app\\hybrid_memory.py" %*' in text, text)
check("pythonw: нет рядом — берём python", sw.pythonw_for("/no/such/python") == "/no/such/python")

# 5. Движок
import hybrid_memory as hm  # noqa: E402

check("версия 1.2.0", hm.VERSION == "1.2.0", hm.VERSION)
check("свой процесс жив", hm._pid_alive(os.getpid()))
check("несуществующий процесс мёртв", not hm._pid_alive(2 ** 22 + 12345))
check("путь от дома через /", hm.rel_to_home(pathlib.Path.home() / "a" / "b.md") == "~/a/b.md")
check("раздел главной памяти узнаётся на обоих языках",
      tuple(hm.CURATED_SECTIONS) == ("## Активная память", "## Active memory"), str(getattr(hm, "CURATED_SECTIONS", None)))

# 6. Язык: выбор при установке, шаблоны, метки в настройках Кодекса
import i18n  # noqa: E402

got = [i18n.normalize(x) for x in ("RU", "en-US", "ru_RU.UTF-8", "C.UTF-8", "de_DE", "english", "")]
check("язык: разбор значений", got == ["ru", "en", "ru", None, None, "en", None], str(got))
saved_env = {key: os.environ.pop(key, None) for key in ("CODEX_MEMORY_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
lang_home = tmp / "lang-home"
lang_home.mkdir(parents=True, exist_ok=True)
os.environ["LANG"] = "de_DE.UTF-8"
check("язык установки: система не русская — английский", i18n.pick_for_install(None, lang_home, legacy=False) == "en")
os.environ["LANG"] = "ru_RU.UTF-8"
check("язык установки: русская система — русский", i18n.pick_for_install(None, lang_home, legacy=False) == "ru")
os.environ["LANG"] = "en_US.UTF-8"
check("язык установки: память старше 1.2.0 остаётся русской", i18n.pick_for_install(None, lang_home, legacy=True) == "ru")
i18n.save("en", lang_home)
check("язык установки: сохранённый выбор важнее", i18n.saved(lang_home) == "en" and i18n.pick_for_install(None, lang_home, legacy=True) == "en")
check("язык установки: флаг важнее сохранённого", i18n.pick_for_install("ru", lang_home, legacy=False) == "ru")
os.environ["CODEX_MEMORY_LANG"] = "ru"
check("язык установки: переменная среды важнее сохранённого", i18n.pick_for_install(None, lang_home, legacy=False) == "ru")
for key, value in saved_env.items():
    os.environ.pop(key, None)
    if value is not None:
        os.environ[key] = value

for code in i18n.SUPPORTED:
    for name in ("AGENTS-block.md", "MEMORY.md"):
        check(f"шаблон {code}/{name}", (app / "templates" / code / name).is_file())
for begin in cc.TOML_BEGINS:
    sample = f'a = 1\n{begin}\n[mcp_servers.extended-memory]\ncommand = "x"\n{cc.TOML_END}\n[other]\nb = 2\n'
    cleaned = cc.remove_server_block(sample)
    check("TOML: наш кусок убирается по метке на любом языке",
          "extended-memory" not in cleaned and "[other]" in cleaned and "a = 1" in cleaned, cleaned)
i18n.use("en")
english = cc.toml_begin() + " " + " ".join(item[3] for item in cc.hooks_spec())
check("английский: метка в config.toml и подписи хуков без кириллицы",
      not any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in english), english)
check("английский: L() отдаёт английский текст", i18n.L("да", "yes") == "yes")
i18n.use("ru")
check("русский: метка в config.toml прежняя", cc.toml_begin() == cc.TOML_BEGINS[0])
check("русский: L() отдаёт русский текст", i18n.L("да", "yes") == "да")

print("\nИТОГ:", "все проверки прошли" if not fails else f"не прошло: {len(fails)} — {fails}")
sys.exit(1 if fails else 0)
