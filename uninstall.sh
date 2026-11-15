#!/usr/bin/env bash
# Отключение расширенной памяти от Кодекса. Данные памяти остаются, если не указать --purge.
set -euo pipefail

MEM_HOME="${CODEX_MEMORY_HOME:-$HOME/.codex-memory}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --memory-home) MEM_HOME="$2"; shift ;;
    --codex-home) CODEX_DIR="$2"; shift ;;
    -h|--help)
      echo "Использование: bash uninstall.sh [--purge] [--memory-home ПАПКА] [--codex-home ПАПКА]"
      echo "  --purge  убрать и сами данные памяти (на macOS — в Корзину)"
      exit 0 ;;
    *) echo "Неизвестный параметр: $1"; exit 1 ;;
  esac
  shift
done

ok()   { printf '  ✓ %s\n' "$*"; }
note() { printf '  ! %s\n' "$*"; }

[ -d "$MEM_HOME" ] || { echo "Память не найдена в $MEM_HOME — нечего удалять."; exit 0; }
if [ ! -f "$MEM_HOME/app/hybrid_memory.py" ] && [ ! -f "$MEM_HOME/MEMORY.md" ]; then
  echo "Папка $MEM_HOME не похожа на расширенную память — ничего не трогаю."; exit 1
fi

PY="$(sed -n 's/^exec "\(.*\)" ".*hybrid_memory.py".*/\1/p' "$MEM_HOME/bin/memory" 2>/dev/null || true)"
[ -n "$PY" ] && [ -x "$PY" ] || PY="$(command -v python3 || echo /usr/bin/python3)"

echo "Отключаю расширенную память от Кодекса…"
if [ -f "$MEM_HOME/app/configure_codex.py" ]; then
  "$PY" "$MEM_HOME/app/configure_codex.py" uninstall --mem-home "$MEM_HOME" --codex-home "$CODEX_DIR"
fi

if [ "$(uname -s)" = "Darwin" ]; then
  for label in local.codex-memory.sync local.codex-memory.maintain; do
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/$label.plist"
  done
  ok "фоновые задачи launchd убраны"
elif command -v crontab >/dev/null 2>&1; then
  CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
  if printf '%s\n' "$CURRENT_CRON" | grep -q '# codex-memory'; then
    printf '%s\n' "$CURRENT_CRON" | grep -v '# codex-memory' | crontab - || true
  fi
  ok "задачи cron убраны"
fi

rm -rf "$MEM_HOME/app" "$MEM_HOME/bin" "$MEM_HOME/.index"
ok "программа и индекс удалены"

if [ "$PURGE" = "1" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  if [ "$(uname -s)" = "Darwin" ] && [ -d "$HOME/.Trash" ]; then
    mv "$MEM_HOME" "$HOME/.Trash/codex-memory-$STAMP" && ok "данные памяти перенесены в Корзину"
  else
    mv "$MEM_HOME" "$MEM_HOME.deleted-$STAMP" && note "данные переименованы в $MEM_HOME.deleted-$STAMP — удалите папку сами"
  fi
else
  ok "заметки и главная память остались в $MEM_HOME (MEMORY.md, memory/, backups/)"
  echo "    Вернуть всё: запустите install.sh снова. Удалить и данные: bash uninstall.sh --purge"
fi
echo "Перезапустите Кодекс."
