#!/usr/bin/env bash
# Установка расширенной памяти для Кодекса (macOS / Linux). Запуск: bash install.sh
# Повторный запуск безопасен: обновляет код, данные памяти не трогает.
# Сообщения — на русском или английском: m 'по-русски' 'in English' (язык выбирает pick_lang).
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEM_HOME="${CODEX_MEMORY_HOME:-$HOME/.codex-memory}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
MODEL="bge-m3"
WITH_HOOKS=1
WITH_EMB=1
WITH_SCHEDULE=1
WITH_HISTORY=1
HISTORY_DAYS=120
LANG_ARG=""
WANT_HELP=0
BAD_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-embeddings) WITH_EMB=0 ;;
    --model) MODEL="$2"; shift ;;
    --no-history) WITH_HISTORY=0 ;;
    --history-days) HISTORY_DAYS="$2"; shift ;;
    --no-hooks) WITH_HOOKS=0 ;;
    --no-schedule) WITH_SCHEDULE=0 ;;
    --memory-home) MEM_HOME="$2"; shift ;;
    --codex-home) CODEX_DIR="$2"; shift ;;
    --lang) LANG_ARG="$2"; shift ;;
    --lang=*) LANG_ARG="${1#--lang=}" ;;
    -h|--help) WANT_HELP=1 ;;
    *) [ -n "$BAD_ARG" ] || BAD_ARG="$1" ;;
  esac
  shift
done
case "$MEM_HOME" in /*) ;; *) MEM_HOME="$PWD/$MEM_HOME" ;; esac
case "$CODEX_DIR" in /*) ;; *) CODEX_DIR="$PWD/$CODEX_DIR" ;; esac

# ------------------------------------------------------------------ язык
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
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
note() { printf '  ! %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

usage() {
  say 'Использование: bash install.sh [параметры]' 'Usage: bash install.sh [options]'
  echo
  say '  --no-embeddings      не включать смысловой поиск (без Ollama, только поиск по словам)' '  --no-embeddings      do not turn on semantic search (no Ollama, keyword search only)'
  say '  --model ИМЯ          модель эмбеддингов Ollama (по умолчанию bge-m3)' '  --model NAME         Ollama embedding model (default: bge-m3)'
  say '  --no-history         не загружать в память прошлые сессии Кодекса' '  --no-history         do not load past Codex sessions into the memory'
  say '  --history-days N     за сколько дней загружать историю (по умолчанию 120)' '  --history-days N     how many days of history to load (default: 120)'
  say '  --no-hooks           без хуков (автоподмешивания памяти и автозаписи)' '  --no-hooks           no hooks (no automatic memory injection, no auto-records)'
  say '  --no-schedule        без фонового обновления индекса' '  --no-schedule        no background index updates'
  say '  --memory-home ПАПКА  где хранить память (по умолчанию ~/.codex-memory)' '  --memory-home DIR    where to keep the memory (default: ~/.codex-memory)'
  say '  --codex-home ПАПКА   папка настроек Кодекса (по умолчанию ~/.codex)' '  --codex-home DIR     Codex settings folder (default: ~/.codex)'
  say '  --lang ru|en         язык сообщений и инструкций для агента (по умолчанию — по языку системы)' '  --lang ru|en         language of the messages and the agent instructions (default: the system language)'
}

if [ -n "$LANG_ARG" ] && [ -z "$(lang_code "$LANG_ARG")" ]; then
  say "Неизвестный язык: $LANG_ARG (нужно ru или en)" "Unknown language: $LANG_ARG (use ru or en)"
  exit 1
fi
if [ -n "$BAD_ARG" ]; then
  say "Неизвестный параметр: $BAD_ARG" "Unknown option: $BAD_ARG"
  usage
  exit 1
fi
if [ "$WANT_HELP" = "1" ]; then usage; exit 0; fi

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) die "$(m 'Этот установщик — для macOS и Linux. На Windows запустите install.cmd из этой же папки (а если Кодекс работает в WSL — bash install.sh внутри WSL).' 'This installer is for macOS and Linux. On Windows run install.cmd from this same folder (or, if Codex runs in WSL, bash install.sh inside WSL).')" ;;
esac

printf '\033[1m%s\033[0m\n' "$(m 'Расширенная память для Кодекса' 'Extended Memory for Codex')"
say "  память:   $MEM_HOME" "  memory:   $MEM_HOME"
say "  Кодекс:   $CODEX_DIR" "  Codex:    $CODEX_DIR"
say '  язык:     русский (другой: --lang en, после установки — memory lang en)' '  language: English (to change: --lang ru, after installing: memory lang ru)'

# ------------------------------------------------------------------ 1. Python
step "1/7 Python"
PY_TEST='import sys, sqlite3
assert sys.version_info >= (3, 8)
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")'
CANDIDATES=""
if [ "$OS" = "Darwin" ]; then
  # системный python3 без Command Line Tools вызывает окно установки — трогаем его, только если CLT есть
  if xcode-select -p >/dev/null 2>&1; then CANDIDATES="/usr/bin/python3"; fi
  CANDIDATES="$CANDIDATES /opt/homebrew/bin/python3 /usr/local/bin/python3"
else
  CANDIDATES="/usr/bin/python3 /usr/local/bin/python3"
fi
if command -v python3 >/dev/null 2>&1; then
  CANDIDATES="$CANDIDATES $(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
fi
PY=""
for c in $CANDIDATES; do
  if [ -x "$c" ] && "$c" -c "$PY_TEST" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  if [ "$OS" = "Darwin" ]; then
    die "$(m 'Нужен Python 3.8+ с SQLite FTS5. Установите: xcode-select --install (или brew install python) и запустите снова.' 'Python 3.8+ with SQLite FTS5 is required. Install it: xcode-select --install (or brew install python), then run this again.')"
  fi
  die "$(m 'Нужен Python 3.8+ с SQLite FTS5 (например: sudo apt install python3) — и запустите снова.' 'Python 3.8+ with SQLite FTS5 is required (for example: sudo apt install python3) — then run this again.')"
fi
ok "$("$PY" -c 'import sys; print("Python " + sys.version.split()[0])') — $PY"

# ------------------------------------------------------------------ 2. Файлы
step "$(m '2/7 Файлы памяти' '2/7 Memory files')"
mkdir -p "$MEM_HOME/app" "$MEM_HOME/bin" "$MEM_HOME/memory" "$MEM_HOME/logs" "$MEM_HOME/backups" "$MEM_HOME/.state"
chmod 700 "$MEM_HOME"
cp -R "$PKG_DIR/app/." "$MEM_HOME/app/"
cp "$PKG_DIR/README.md" "$MEM_HOME/README.md" 2>/dev/null || true
cp "$PKG_DIR/README.ru.md" "$MEM_HOME/README.ru.md" 2>/dev/null || true
cp "$PKG_DIR/uninstall.sh" "$MEM_HOME/uninstall.sh" 2>/dev/null || true
chmod +x "$MEM_HOME/app/"*.py "$MEM_HOME/uninstall.sh" 2>/dev/null || true
if [ "$OS" = "Darwin" ]; then xattr -dr com.apple.quarantine "$MEM_HOME" 2>/dev/null || true; fi
printf '%s\n' "$UI_LANG" > "$MEM_HOME/.state/lang"
cat > "$MEM_HOME/bin/memory" <<EOF
#!/bin/sh
# Extended memory for Codex: command line. Usage: memory --help
export CODEX_MEMORY_HOME="$MEM_HOME"
exec "$PY" "$MEM_HOME/app/hybrid_memory.py" "\$@"
EOF
chmod +x "$MEM_HOME/bin/memory"
MEM="$MEM_HOME/bin/memory"
ok "$(m "код установлен в $MEM_HOME/app" "code installed to $MEM_HOME/app")"
if [ ! -f "$MEM_HOME/MEMORY.md" ]; then
  cp "$MEM_HOME/app/templates/$UI_LANG/MEMORY.md" "$MEM_HOME/MEMORY.md"
  chmod 600 "$MEM_HOME/MEMORY.md"
  ok "$(m 'создана главная память MEMORY.md' 'created the main memory file MEMORY.md')"
else
  ok "$(m 'MEMORY.md уже есть — не трогаю' 'MEMORY.md already exists — leaving it as is')"
fi
ok "$(m "командная строка: $MEM" "command line: $MEM")"

# ------------------------------------------------------------------ 3. Ollama
step "$(m "3/7 Смысловой поиск (Ollama + $MODEL)" "3/7 Semantic search (Ollama + $MODEL)")"
OLLAMA_URL="http://127.0.0.1:11434"
ollama_up() { curl -s --noproxy '*' --max-time 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; }
find_ollama() {
  for c in "$(command -v ollama 2>/dev/null || true)" /opt/homebrew/bin/ollama /usr/local/bin/ollama \
           /Applications/Ollama.app/Contents/Resources/ollama; do
    if [ -n "$c" ] && [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  return 1
}
EMB_ON=0
if [ "$WITH_EMB" = "1" ]; then
  if OLLAMA_BIN="$(find_ollama)"; then
    if ! ollama_up; then
      say '  Запускаю Ollama…' '  Starting Ollama…'
      if [ "$OS" = "Darwin" ] && [ -d /Applications/Ollama.app ]; then
        open -ga Ollama || true
      elif [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1 && brew list ollama >/dev/null 2>&1; then
        brew services start ollama >/dev/null 2>&1 || true
      else
        nohup "$OLLAMA_BIN" serve >"$MEM_HOME/logs/ollama.log" 2>&1 &
      fi
      i=0
      while [ $i -lt 30 ] && ! ollama_up; do sleep 1; i=$((i + 1)); done
    fi
    if ollama_up; then
      if curl -s --noproxy '*' --max-time 5 "$OLLAMA_URL/api/tags" | grep -q "\"$MODEL"; then
        ok "$(m "модель $MODEL уже скачана" "the $MODEL model is already downloaded")"
      else
        say "  Скачиваю модель $MODEL (около 1,2 ГБ, один раз)…" "  Downloading the $MODEL model (about 1.2 GB, once)…"
        "$OLLAMA_BIN" pull "$MODEL" || note "$(m "не удалось скачать $MODEL" "could not download $MODEL")"
      fi
      if "$MEM" embeddings on --model "$MODEL" >/dev/null 2>&1; then EMB_ON=1; fi
    else
      note "$(m 'Ollama установлена, но не отвечает' 'Ollama is installed but does not respond')"
    fi
  else
    note "$(m 'Ollama не найдена' 'Ollama not found')"
  fi
fi
if [ "$EMB_ON" = "1" ]; then
  ok "$(m "смысловой поиск включён ($MODEL)" "semantic search is on ($MODEL)")"
else
  "$MEM" embeddings off >/dev/null 2>&1 || true
  note "$(m 'пока работает поиск только по словам. Для поиска по смыслу установите Ollama' 'for now only keyword search works. For semantic search, install Ollama')"
  note "$(m '(brew install ollama или https://ollama.com) и запустите install.sh ещё раз.' '(brew install ollama or https://ollama.com) and run install.sh again.')"
fi

# ------------------------------------------------------------------ 4. Кодекс
step "$(m '4/7 Подключение к Кодексу' '4/7 Connecting to Codex')"
HOOK_FLAG=""
if [ "$WITH_HOOKS" != "1" ]; then HOOK_FLAG="--no-hooks"; fi
# shellcheck disable=SC2086
"$PY" "$MEM_HOME/app/configure_codex.py" install --mem-home "$MEM_HOME" --codex-home "$CODEX_DIR" --python "$PY" --lang "$UI_LANG" $HOOK_FLAG

# ------------------------------------------------------------------ 5. История
step "$(m '5/7 История прошлых сессий Кодекса' '5/7 History of past Codex sessions')"
if [ "$WITH_HISTORY" = "1" ]; then
  if HIST="$("$MEM" import-codex-history --days "$HISTORY_DAYS" --codex-home "$CODEX_DIR" 2>&1)"; then
    ok "$HIST"
  else
    note "$(m "историю загрузить не удалось: $HIST" "could not load the history: $HIST")"
  fi
else
  ok "$(m 'пропущено (--no-history)' 'skipped (--no-history)')"
fi

# ------------------------------------------------------------------ 6. Индекс
step "$(m '6/7 Первая индексация' '6/7 First indexing')"
if "$MEM" sync --text-only --quiet; then
  COUNTS="$("$MEM" status --json | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(str(d['files']) + ' ' + str(d['chunks']))")"
  FILES_N="${COUNTS%% *}"
  CHUNKS_N="${COUNTS##* }"
  ok "$(m "поиск по словам готов: файлов $FILES_N, фрагментов $CHUNKS_N" "keyword search is ready: files $FILES_N, fragments $CHUNKS_N")"
else
  note "$(m "индексация завершилась с ошибкой — проверьте: $MEM doctor" "indexing ended with an error — check with: $MEM doctor")"
fi

# ------------------------------------------------------------------ 7. Фон
step "$(m '7/7 Фоновое обновление индекса' '7/7 Background index updates')"
if [ "$WITH_SCHEDULE" = "1" ]; then
  if [ "$OS" = "Darwin" ]; then
    LA="$HOME/Library/LaunchAgents"
    mkdir -p "$LA"
    # $1 — метка, $2 — команда memory, $3 — доп. аргумент, $4 — расписание (xml)
    write_plist() {
      cat > "$LA/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>$MEM</string><string>$2</string><string>$3</string></array>
  $4
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEX_MEMORY_HOME</key><string>$MEM_HOME</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$MEM_HOME/logs/$2.log</string>
  <key>StandardErrorPath</key><string>$MEM_HOME/logs/$2.log</string>
</dict>
</plist>
EOF
    }
    write_plist local.codex-memory.sync sync --quiet "<key>StartInterval</key><integer>1800</integer><key>RunAtLoad</key><true/>"
    write_plist local.codex-memory.maintain maintain --json "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>"
    for label in local.codex-memory.sync local.codex-memory.maintain; do
      launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
      loaded=0
      # bootout завершается не мгновенно, поэтому несколько попыток
      for attempt in 1 2 3 4 5; do
        if launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist" >/dev/null 2>&1; then loaded=1; break; fi
        sleep 1
      done
      [ "$loaded" = "1" ] || note "$(m "не удалось запустить $label (проверьте: launchctl print gui/$(id -u)/$label)" "could not start $label (check: launchctl print gui/$(id -u)/$label)")"
    done
    ok "$(m 'launchd: обновление каждые 30 минут, чистка в 04:30' 'launchd: index update every 30 minutes, cleanup at 04:30')"
    [ "$EMB_ON" = "1" ] && ok "$(m "векторы досчитываются в фоне (прогресс: $MEM status)" "vectors are being computed in the background (progress: $MEM status)")"
  elif command -v crontab >/dev/null 2>&1; then
    # сначала прочитать целиком, потом писать
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
    { if [ -n "$CURRENT_CRON" ]; then printf '%s\n' "$CURRENT_CRON" | grep -v '# codex-memory' || true; fi
      echo "*/30 * * * * \"$MEM\" sync --quiet >> \"$MEM_HOME/logs/sync.log\" 2>&1 # codex-memory"
      echo "30 4 * * * \"$MEM\" maintain >> \"$MEM_HOME/logs/maintain.log\" 2>&1 # codex-memory"
    } | crontab -
    ok "$(m 'cron: обновление каждые 30 минут, чистка в 04:30' 'cron: index update every 30 minutes, cleanup at 04:30')"
    if [ "$EMB_ON" = "1" ]; then
      nohup "$MEM" sync --quiet >>"$MEM_HOME/logs/sync.log" 2>&1 &
      ok "$(m "векторы досчитываются в фоне (прогресс: $MEM status)" "vectors are being computed in the background (progress: $MEM status)")"
    fi
  else
    note "$(m "нет crontab — запускайте иногда вручную: $MEM sync" "no crontab — run this by hand from time to time: $MEM sync")"
  fi
else
  ok "$(m 'пропущено (--no-schedule)' 'skipped (--no-schedule)')"
  if [ "$EMB_ON" = "1" ]; then nohup "$MEM" sync --quiet >>"$MEM_HOME/logs/sync.log" 2>&1 & fi
fi

# ------------------------------------------------------------------ итог
printf '\n\033[1m%s\033[0m %s\n' "$(m 'Готово!' 'Done!')" "$(m 'Что дальше:' 'What next:')"
say '  1. Полностью перезапустите Кодекс (приложение — Cmd+Q и открыть снова; CLI — новая сессия).' '  1. Restart Codex completely (the app: Cmd+Q and open it again; the CLI: start a new session).'
if [ "$WITH_HOOKS" = "1" ]; then
  say '  2. При первом запуске Кодекс спросит про новые хуки (Hooks need review) —' '  2. On first launch Codex will ask about the new hooks (Hooks need review):'
  say '     выберите «Trust all and continue». Без этого память не подмешивается и не автосохраняется.' '     choose "Trust all and continue". Without it the memory is neither added to sessions nor saved automatically.'
fi
say "  3. Расскажите о себе: «запомни обо мне: …» — или заполните раздел «Обо мне» в $MEM_HOME/MEMORY.md" "  3. Tell it about yourself: \"remember about me: …\" — or fill in the \"About me\" section of $MEM_HOME/MEMORY.md"
say "  4. Проверка: $MEM doctor" "  4. Check: $MEM doctor"
if pgrep -f "Codex.app/Contents/MacOS" >/dev/null 2>&1 || pgrep -x codex >/dev/null 2>&1; then
  note "$(m 'Кодекс сейчас запущен — перезапустите его, чтобы он увидел память.' 'Codex is running right now — restart it so that it sees the memory.')"
fi
