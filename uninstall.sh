#!/usr/bin/env bash
# Отключение расширенной памяти от Кодекса. Данные памяти остаются, если не указать --purge.
# Сообщения — на русском или английском: m 'по-русски' 'in English' (язык выбирает pick_lang).
set -euo pipefail

MEM_HOME="${CODEX_MEMORY_HOME:-$HOME/.codex-memory}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
PURGE=0
LANG_ARG=""
WANT_HELP=0
BAD_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --memory-home) MEM_HOME="$2"; shift ;;
    --codex-home) CODEX_DIR="$2"; shift ;;
    --lang) LANG_ARG="$2"; shift ;;
    --lang=*) LANG_ARG="${1#--lang=}" ;;
    -h|--help) WANT_HELP=1 ;;
    *) [ -n "$BAD_ARG" ] || BAD_ARG="$1" ;;
  esac
  shift
done

# «ru», «RU», «ru_RU.UTF-8», «english» → ru или en; непонятное — пустая строка
lang_code() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    ru|ru[_.@-]*|rus|russian) echo ru ;;
    en|en[_.@-]*|eng|english) echo en ;;
  esac
}

# Флаг --lang → CODEX_MEMORY_LANG → сохранённый выбор → русский для памяти,
# поставленной до версии 1.2.0 → язык системы → английский.
pick_lang() {
  local value code
  for value in "$LANG_ARG" "${CODEX_MEMORY_LANG:-}" "$(cat "$MEM_HOME/.state/lang" 2>/dev/null || true)"; do
    code="$(lang_code "$value")"
    if [ -n "$code" ]; then echo "$code"; return; fi
  done
  if [ -f "$MEM_HOME/MEMORY.md" ] || [ -f "$MEM_HOME/app/hybrid_memory.py" ]; then echo ru; return; fi
  for value in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
    case "$value" in
      ""|C|C.*|POSIX|POSIX.*) ;;
      *) code="$(lang_code "$value")"; echo "${code:-en}"; return ;;
    esac
  done
  if [ "$(uname -s)" = "Darwin" ]; then
    code="$(lang_code "$(defaults read -g AppleLocale 2>/dev/null || true)")"
    if [ -n "$code" ]; then echo "$code"; return; fi
  fi
  echo en
}

UI_LANG="$(pick_lang)"
export CODEX_MEMORY_LANG="$UI_LANG"

m()    { if [ "$UI_LANG" = "en" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
say()  { printf '%s\n' "$(m "$1" "$2")"; }
ok()   { printf '  ✓ %s\n' "$*"; }
note() { printf '  ! %s\n' "$*"; }

usage() {
  say 'Использование: bash uninstall.sh [--purge] [--memory-home ПАПКА] [--codex-home ПАПКА] [--lang ru|en]' 'Usage: bash uninstall.sh [--purge] [--memory-home DIR] [--codex-home DIR] [--lang ru|en]'
  say '  --purge  убрать и сами данные памяти (на macOS — в Корзину)' '  --purge  remove the memory data as well (on macOS it goes to the Trash)'
}

if [ -n "$LANG_ARG" ] && [ -z "$(lang_code "$LANG_ARG")" ]; then
  say "Неизвестный язык: $LANG_ARG (нужно ru или en)" "Unknown language: $LANG_ARG (use ru or en)"
  exit 1
fi
if [ -n "$BAD_ARG" ]; then
  say "Неизвестный параметр: $BAD_ARG" "Unknown option: $BAD_ARG"
  exit 1
fi
if [ "$WANT_HELP" = "1" ]; then usage; exit 0; fi

if [ ! -d "$MEM_HOME" ]; then
  say "Память не найдена в $MEM_HOME — нечего удалять." "No memory found in $MEM_HOME — nothing to remove."
  exit 0
fi
if [ ! -f "$MEM_HOME/app/hybrid_memory.py" ] && [ ! -f "$MEM_HOME/MEMORY.md" ]; then
  say "Папка $MEM_HOME не похожа на расширенную память — ничего не трогаю." "The $MEM_HOME folder does not look like extended memory — leaving it untouched."
  exit 1
fi

PY="$(sed -n 's/^exec "\(.*\)" ".*hybrid_memory.py".*/\1/p' "$MEM_HOME/bin/memory" 2>/dev/null || true)"
[ -n "$PY" ] && [ -x "$PY" ] || PY="$(command -v python3 || echo /usr/bin/python3)"

say 'Отключаю расширенную память от Кодекса…' 'Disconnecting extended memory from Codex…'
if [ -f "$MEM_HOME/app/configure_codex.py" ]; then
  "$PY" "$MEM_HOME/app/configure_codex.py" uninstall --mem-home "$MEM_HOME" --codex-home "$CODEX_DIR"
fi

if [ "$(uname -s)" = "Darwin" ]; then
  for label in local.codex-memory.sync local.codex-memory.maintain; do
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/$label.plist"
  done
  ok "$(m 'фоновые задачи launchd убраны' 'launchd background tasks removed')"
elif command -v crontab >/dev/null 2>&1; then
  CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
  if printf '%s\n' "$CURRENT_CRON" | grep -q '# codex-memory'; then
    printf '%s\n' "$CURRENT_CRON" | grep -v '# codex-memory' | crontab - || true
  fi
  ok "$(m 'задачи cron убраны' 'cron tasks removed')"
fi

rm -rf "$MEM_HOME/app" "$MEM_HOME/bin" "$MEM_HOME/.index"
ok "$(m 'программа и индекс удалены' 'program and index removed')"

if [ "$PURGE" = "1" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  if [ "$(uname -s)" = "Darwin" ] && [ -d "$HOME/.Trash" ]; then
    mv "$MEM_HOME" "$HOME/.Trash/codex-memory-$STAMP" && ok "$(m 'данные памяти перенесены в Корзину' 'memory data moved to the Trash')"
  else
    mv "$MEM_HOME" "$MEM_HOME.deleted-$STAMP" && note "$(m "данные переименованы в $MEM_HOME.deleted-$STAMP — удалите папку сами" "the data folder was renamed to $MEM_HOME.deleted-$STAMP — delete it yourself")"
  fi
else
  ok "$(m "заметки и главная память остались в $MEM_HOME (MEMORY.md, memory/, backups/)" "your notes and the main memory stay in $MEM_HOME (MEMORY.md, memory/, backups/)")"
  say '    Вернуть всё: запустите install.sh снова. Удалить и данные: bash uninstall.sh --purge' '    To bring it back, run install.sh again. To delete the data too: bash uninstall.sh --purge'
fi
say 'Перезапустите Кодекс.' 'Restart Codex.'
