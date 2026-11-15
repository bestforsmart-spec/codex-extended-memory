"""Сборка архива пакета для скачивания.

usage: build_zip.py <корень репозитория> <выходной zip>

В архив попадает только сам пакет (список PACKAGE) в папке codex-extended-memory/:
тесты, инструменты и служебные файлы репозитория остаются снаружи.
Права: каталоги 755, install.sh и uninstall.sh — 755, остальные файлы — 644.
"""
import pathlib
import sys
import time
import zipfile

PACKAGE = ["README.md", "install.sh", "uninstall.sh", "install.cmd", "uninstall.cmd", "app"]
TOP = "codex-extended-memory"
EXECUTABLE = {"install.sh", "uninstall.sh"}


def collect(root):
    """Пары (путь на диске, имя в архиве) для всего, что входит в пакет."""
    items = [(root, TOP + "/")]
    for name in PACKAGE:
        path = root / name
        assert path.exists(), f"нет файла пакета: {name}"
        if path.is_file():
            items.append((path, f"{TOP}/{name}"))
            continue
        items.append((path, f"{TOP}/{name}/"))
        for child in sorted(path.rglob("*")):
            rel = child.relative_to(root)
            if "__pycache__" in rel.parts or child.suffix == ".pyc" or child.name == ".DS_Store":
                continue
            items.append((child, f"{TOP}/{rel.as_posix()}" + ("/" if child.is_dir() else "")))
    return items


def main():
    root = pathlib.Path(sys.argv[1]).resolve()
    out = pathlib.Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    items = collect(root)
    with zipfile.ZipFile(out, "w") as archive:
        for path, name in items:
            info = zipfile.ZipInfo(name, time.localtime(path.stat().st_mtime)[:6])
            info.create_system = 3  # права в формате Unix — чтобы install.sh остался исполняемым
            if name.endswith("/"):
                info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, b"")
            else:
                mode = 0o755 if path.name in EXECUTABLE else 0o644
                info.external_attr = (0o100000 | mode) << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    print(f"{out.name}: {out.stat().st_size} байт, записей {len(items)}")


if __name__ == "__main__":
    main()
