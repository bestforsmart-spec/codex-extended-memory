#!/usr/bin/env bash
# Установка расширенной памяти для Кодекса (macOS / Linux). Запуск: bash install.sh
# Повторный запуск безопасен: обновляет код, данные памяти не трогает.
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

usage() {
  cat <<'EOF'
Использование: bash install.sh [параметры]

  --no-embeddings      не включать смысловой поиск (без Ollama, только поиск по словам)
  --model ИМЯ          модель эмбеддингов Ollama (по умолчанию bge-m3)
  --no-history         не загружать в память прошлые сессии Кодекса
  --history-days N     за сколько дней загружать историю (по умолчанию 120)
  --no-hooks           без хуков (автоподмешивания памяти и автозаписи)
  --no-schedule        без фонового обновления индекса
  --memory-home ПАПКА  где хранить память (по умолчанию ~/.codex-memory)
  --codex-home ПАПКА   папка настроек Кодекса (по умолчанию ~/.codex)
EOF
}

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
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $1"; usage; exit 1 ;;
  esac
  shift
done

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
note() { printf '  ! %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) die "Этот установщик — для macOS и Linux. На Windows запустите install.cmd из этой же папки (а если Кодекс работает в WSL — bash install.sh внутри WSL)." ;;
esac
case "$MEM_HOME" in /*) ;; *) MEM_HOME="$PWD/$MEM_HOME" ;; esac
case "$CODEX_DIR" in /*) ;; *) CODEX_DIR="$PWD/$CODEX_DIR" ;; esac

printf '\033[1mРасширенная память для Кодекса\033[0m\n'
echo "  память:   $MEM_HOME"
echo "  Кодекс:   $CODEX_DIR"

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
    die "Нужен Python 3.8+ с SQLite FTS5. Установите: xcode-select --install (или brew install python) и запустите снова."
  fi
  die "Нужен Python 3.8+ с SQLite FTS5 (например: sudo apt install python3) — и запустите снова."
fi
ok "$("$PY" -c 'import sys; print("Python " + sys.version.split()[0])') — $PY"

# ------------------------------------------------------------------ 2. Файлы
step "2/7 Файлы памяти"
mkdir -p "$MEM_HOME/app" "$MEM_HOME/bin" "$MEM_HOME/memory" "$MEM_HOME/logs" "$MEM_HOME/backups" "$MEM_HOME/.state"
chmod 700 "$MEM_HOME"
cp -R "$PKG_DIR/app/." "$MEM_HOME/app/"
cp "$PKG_DIR/README.md" "$MEM_HOME/README.md" 2>/dev/null || true
cp "$PKG_DIR/uninstall.sh" "$MEM_HOME/uninstall.sh" 2>/dev/null || true
chmod +x "$MEM_HOME/app/"*.py "$MEM_HOME/uninstall.sh" 2>/dev/null || true
if [ "$OS" = "Darwin" ]; then xattr -dr com.apple.quarantine "$MEM_HOME" 2>/dev/null || true; fi
cat > "$MEM_HOME/bin/memory" <<EOF
#!/bin/sh
# Командная строка расширенной памяти: memory --help
export CODEX_MEMORY_HOME="$MEM_HOME"
exec "$PY" "$MEM_HOME/app/hybrid_memory.py" "\$@"
EOF
chmod +x "$MEM_HOME/bin/memory"
MEM="$MEM_HOME/bin/memory"
ok "код установлен в $MEM_HOME/app"
if [ ! -f "$MEM_HOME/MEMORY.md" ]; then
  cp "$MEM_HOME/app/templates/MEMORY.md" "$MEM_HOME/MEMORY.md"
  chmod 600 "$MEM_HOME/MEMORY.md"
  ok "создана главная память MEMORY.md"
else
  ok "MEMORY.md уже есть — не трогаю"
fi
ok "командная строка: $MEM"

# ------------------------------------------------------------------ 3. Ollama
step "3/7 Смысловой поиск (Ollama + $MODEL)"
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
      echo "  Запускаю Ollama…"
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
        ok "модель $MODEL уже скачана"
      else
        echo "  Скачиваю модель $MODEL (около 1,2 ГБ, один раз)…"
        "$OLLAMA_BIN" pull "$MODEL" || note "не удалось скачать $MODEL"
      fi
      if "$MEM" embeddings on --model "$MODEL" >/dev/null 2>&1; then EMB_ON=1; fi
    else
      note "Ollama установлена, но не отвечает"
    fi
  else
    note "Ollama не найдена"
  fi
fi
if [ "$EMB_ON" = "1" ]; then
  ok "смысловой поиск включён ($MODEL)"
else
  "$MEM" embeddings off >/dev/null 2>&1 || true
  note "пока работает поиск только по словам. Для поиска по смыслу установите Ollama"
  note "(brew install ollama или https://ollama.com) и запустите install.sh ещё раз."
fi

# ------------------------------------------------------------------ 4. Кодекс
step "4/7 Подключение к Кодексу"
HOOK_FLAG=""
if [ "$WITH_HOOKS" != "1" ]; then HOOK_FLAG="--no-hooks"; fi
# shellcheck disable=SC2086
"$PY" "$MEM_HOME/app/configure_codex.py" install --mem-home "$MEM_HOME" --codex-home "$CODEX_DIR" --python "$PY" $HOOK_FLAG

# ------------------------------------------------------------------ 5. История
step "5/7 История прошлых сессий Кодекса"
if [ "$WITH_HISTORY" = "1" ]; then
  if HIST="$("$MEM" import-codex-history --days "$HISTORY_DAYS" --codex-home "$CODEX_DIR" 2>&1)"; then
    ok "$HIST"
  else
    note "историю загрузить не удалось: $HIST"
  fi
else
  ok "пропущено (--no-history)"
fi

# ------------------------------------------------------------------ 6. Индекс
step "6/7 Первая индексация"
if "$MEM" sync --text-only --quiet; then
  ok "поиск по словам готов: $("$MEM" status --json | "$PY" -c "import json,sys; d=json.load(sys.stdin); print('файлов ' + str(d['files']) + ', фрагментов ' + str(d['chunks']))")"
else
  note "индексация завершилась с ошибкой — проверьте: $MEM doctor"
fi

# ------------------------------------------------------------------ 7. Фон
step "7/7 Фоновое обновление индекса"
if [ "$WITH_SCHEDULE" = "1" ]; then
  if [ "$OS" = "Darwin" ]; then
    LA="$HOME/Library/LaunchAgents"
    mkdir -p "$LA"
    write_plist() {  # $1 — метка, $2 — команда memory, $3 — доп. аргумент, $4 — расписание (xml)
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
      for attempt in 1 2 3 4 5; do  # bootout завершается не мгновенно
        if launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist" >/dev/null 2>&1; then loaded=1; break; fi
        sleep 1
      done
      [ "$loaded" = "1" ] || note "не удалось запустить $label (проверьте: launchctl print gui/$(id -u)/$label)"
    done
    ok "launchd: обновление каждые 30 минут, чистка в 04:30"
    [ "$EMB_ON" = "1" ] && ok "векторы досчитываются в фоне (прогресс: $MEM status)"
  elif command -v crontab >/dev/null 2>&1; then
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"  # сначала прочитать целиком, потом писать
    { if [ -n "$CURRENT_CRON" ]; then printf '%s\n' "$CURRENT_CRON" | grep -v '# codex-memory' || true; fi
      echo "*/30 * * * * \"$MEM\" sync --quiet >> \"$MEM_HOME/logs/sync.log\" 2>&1 # codex-memory"
      echo "30 4 * * * \"$MEM\" maintain >> \"$MEM_HOME/logs/maintain.log\" 2>&1 # codex-memory"
    } | crontab -
    ok "cron: обновление каждые 30 минут, чистка в 04:30"
    if [ "$EMB_ON" = "1" ]; then
      nohup "$MEM" sync --quiet >>"$MEM_HOME/logs/sync.log" 2>&1 &
      ok "векторы досчитываются в фоне (прогресс: $MEM status)"
    fi
  else
    note "нет crontab — запускайте иногда вручную: $MEM sync"
  fi
else
  ok "пропущено (--no-schedule)"
  if [ "$EMB_ON" = "1" ]; then nohup "$MEM" sync --quiet >>"$MEM_HOME/logs/sync.log" 2>&1 & fi
fi

# ------------------------------------------------------------------ итог
printf '\n\033[1mГотово!\033[0m Что дальше:\n'
echo "  1. Полностью перезапустите Кодекс (приложение — Cmd+Q и открыть снова; CLI — новая сессия)."
if [ "$WITH_HOOKS" = "1" ]; then
  echo "  2. При первом запуске Кодекс спросит про новые хуки (Hooks need review) —"
  echo "     выберите «Trust all and continue». Без этого память не подмешивается и не автосохраняется."
fi
echo "  3. Расскажите о себе: «запомни обо мне: …» — или заполните раздел «Обо мне» в $MEM_HOME/MEMORY.md"
echo "  4. Проверка: $MEM doctor"
if pgrep -f "Codex.app/Contents/MacOS" >/dev/null 2>&1 || pgrep -x codex >/dev/null 2>&1; then
  note "Кодекс сейчас запущен — перезапустите его, чтобы он увидел память."
fi
