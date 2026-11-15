#!/usr/bin/env python3
"""Хук Stop: страховочная сеть памяти.

После каждого значимого хода (что-то сделано, настроено, исправлено, решено) коротко записывает
«запрос → итог» в memory/auto/ГГГГ-ММ-ДД.md, даже если агент забыл вызвать memory_remember.
Никогда не мешает Кодексу: при любой ошибке молча выходит с кодом 0.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TAIL_BYTES = 3 * 1024 * 1024


def latest_user_message(hm, transcript_path):  # type: ignore[no-untyped-def]
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        tail = fh.read().decode("utf-8", errors="ignore")
    latest = ""
    for line in tail.splitlines():
        if '"user_message"' not in line and '"role":"user"' not in line and '"role": "user"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = hm.extract_user_text(str(payload.get("message") or ""))
        elif row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            text = hm.extract_user_text(payload.get("content"))
        else:
            continue
        if text:
            latest = text
    return latest


def main() -> None:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")  # байтами: на Windows stdin не в UTF-8
    data = json.loads(raw) if raw.strip() else {}
    import hybrid_memory as hm

    hm.fix_stdio()
    hm.record_hook_run("Stop")
    turn_id = str(data.get("turn_id") or "") or hm.sha1_text(raw)
    state = hm._load_id_state("stop-hook.json")
    seen = state.get("turns", {})
    if turn_id in seen:
        return
    seen[turn_id] = int(time.time())
    if len(seen) > 1000:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-500:])
    hm._save_id_state("stop-hook.json", {"turns": seen})

    assistant = str(data.get("last_assistant_message") or "")
    user = latest_user_message(hm, data.get("transcript_path"))
    if not hm.is_significant(user, assistant):
        return
    target = hm.append_turn(hm.AUTO_DIR, hm.now_local(), str(data.get("cwd") or ""), user, assistant, "Автозаписи Codex")
    hm.spawn_background_sync([target])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    try:
        sys.stdout.write(json.dumps({"continue": True}))
        sys.stdout.flush()
    except Exception:
        pass
    sys.exit(0)
