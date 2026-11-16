#!/usr/bin/env python3
"""MCP-сервер расширенной памяти (stdio, JSON-RPC 2.0). Только стандартная библиотека.

Кодекс запускает его сам по записи [mcp_servers.extended-memory] в ~/.codex/config.toml.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hybrid_memory as hm  # noqa: E402

L = hm.L  # сообщение на языке памяти: L("по-русски", "in English")

# stdout принадлежит протоколу: любой случайный print уходит в stderr
hm.fix_stdio()
_PROTO_OUT = sys.stdout.buffer
sys.stdout = sys.stderr

FALLBACK_PROTOCOL = "2025-06-18"
INSTRUCTIONS = L(
    "Долговременная память пользователя между сессиями. В начале нетривиальной задачи ищи прошлый контекст "
    "через memory_search (короткий запрос из ключевых слов), подробности — memory_show. Долговечные итоги "
    "(решения и почему, состояние проектов, предпочтения) сохраняй через memory_remember, самое важное и "
    "устойчивое — через memory_curate. Секреты не сохраняй никогда.",
    "The user's long-term memory between sessions. At the start of a non-trivial task, look for past context "
    "with memory_search (a short keyword query); for details use memory_show. Save lasting results "
    "(decisions and why, the state of projects, preferences) with memory_remember, and the most important and "
    "stable ones with memory_curate. Never save secrets.",
)

TOOLS = [
    {
        "name": "memory_search",
        "title": L("Поиск по памяти", "Search the memory"),
        "description": L(
            "Ищет в долговременной памяти: главная память MEMORY.md, дневники, история прошлых сессий, "
            "подключённые папки с заметками. Гибридный поиск по словам и по смыслу. Запрос — несколько "
            "ключевых слов (проект, сервис, клиент, ошибка, решение).",
            "Searches the long-term memory: the main memory MEMORY.md, journals, the history of past sessions, "
            "connected notes folders. Hybrid search by keywords and by meaning. The query is a few "
            "keywords (project, service, client, error, decision).",
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": L("Короткий запрос из ключевых слов.", "A short keyword query.")},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_show",
        "title": L("Открыть фрагмент памяти", "Open a memory fragment"),
        "description": L(
            "Показывает строки файла памяти по пути и номерам строк из результата memory_search.",
            "Shows lines of a memory file by the path and line numbers from a memory_search result.",
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": L(
                        "Путь из результата поиска, например memory/2026-09-25.md",
                        "A path from a search result, for example memory/2026-09-25.md",
                    ),
                },
                "start": {"type": "integer", "minimum": 1, "default": 1},
                "end": {
                    "type": "integer",
                    "minimum": 1,
                    "description": L("По умолчанию start+80, максимум start+400.", "start+80 by default, start+400 at most."),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_remember",
        "title": L("Запомнить (дневник)", "Remember (journal)"),
        "description": L(
            "Записывает короткий долговечный факт в дневник памяти за сегодня: принятое решение и почему, "
            "что сделано, состояние проекта, следующий шаг, предпочтение пользователя. Не для секретов и шума.",
            "Writes a short lasting fact to today's memory journal: a decision and why it was made, "
            "what was done, the state of a project, the next step, a user preference. Not for secrets or noise.",
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "maxLength": 4000,
                    "description": L("Факт одной-тремя фразами. Без секретов.", "A fact in one to three sentences. No secrets."),
                },
                "source": {
                    "type": "string",
                    "maxLength": 300,
                    "description": L("Необязательно: проект или контекст.", "Optional: the project or context."),
                },
            },
            "required": ["note"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_curate",
        "title": L("Запомнить в главную память", "Remember in the main memory"),
        "description": L(
            "Добавляет высокосигнальный факт в главную память MEMORY.md, которая видна в начале КАЖДОЙ сессии: "
            "устойчивые правила работы, долгосрочные предпочтения, ключевые факты о проектах и людях, важные "
            "решения. Держи коротким; сначала проверь memory_search, нет ли уже такого.",
            "Adds a high-signal fact to the main memory MEMORY.md, which is visible at the start of EVERY session: "
            "stable working rules, long-term preferences, key facts about projects and people, important "
            "decisions. Keep it short; first check with memory_search that it is not there already.",
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": L("Один факт одной строкой. Без секретов.", "One fact in one line. No secrets."),
                },
                "category": {
                    "type": "string",
                    "enum": ["preference", "workflow", "project", "infrastructure", "decision", "person", "other"],
                    "default": "other",
                },
                "source": {"type": "string", "maxLength": 300},
            },
            "required": ["note"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_forget",
        "title": L("Забыть", "Forget"),
        "description": L(
            "Удаляет устаревший или ошибочный пункт памяти по фрагменту его текста (копия уходит в "
            ".state/forgotten.md). По умолчанию только в MEMORY.md; scope=all — ещё и в дневниках.",
            "Deletes an outdated or wrong memory item by a fragment of its text (a copy goes to "
            ".state/forgotten.md). By default only in MEMORY.md; scope=all — in the journals too.",
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 4,
                    "description": L("Точный фрагмент текста пункта.", "An exact fragment of the item's text."),
                },
                "scope": {"type": "string", "enum": ["curated", "all"], "default": "curated"},
                "all_matches": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_status",
        "title": L("Состояние памяти", "Memory status"),
        "description": L(
            "Показывает состояние индекса памяти: сколько файлов и фрагментов, работает ли смысловой поиск.",
            "Shows the state of the memory index: how many files and fragments, whether semantic search works.",
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_sync",
        "title": L("Обновить индекс памяти", "Update the memory index"),
        "description": L(
            "Переиндексирует память (нужно редко: индекс обновляется сам после записи и раз в полчаса).",
            "Reindexes the memory (rarely needed: the index updates on its own after a write and every half hour).",
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _sync_note(result: Dict[str, Any]) -> str:
    if result.get("busy"):
        return L("индекс обновляется фоновой задачей", "the index is being updated by a background task")
    if not result.get("ok"):
        return L(f"индекс обновится позже ({result.get('error')})", f"the index will be updated later ({result.get('error')})")
    if result.get("embedding_error"):
        return L(
            "проиндексировано по словам; смысловой индекс догонит позже",
            "indexed by keywords; the semantic index will catch up later",
        )
    return L("проиндексировано", "indexed")


def tool_search(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError(L("пустой запрос", "empty query"))
    return hm.format_search(hm.search(query, int(args.get("limit") or 8)))


def tool_show(args: Dict[str, Any]) -> str:
    start = int(args.get("start") or 1)
    end = args.get("end")
    return hm.show(str(args.get("path") or ""), start, int(end) if end else None)


def tool_remember(args: Dict[str, Any]) -> str:
    result = hm.remember(str(args.get("note") or ""), args.get("source"))
    if not result.get("saved"):
        return L(
            "Такой пункт в сегодняшнем дневнике уже есть — не дублирую.",
            "Today's journal already has this item — not duplicating it.",
        )
    sync = hm.try_sync(only=[Path(result["path"])], max_seconds=45)
    return L(
        f"Сохранено в {hm.rel_to_home(Path(result['path']))} ({_sync_note(sync)}).",
        f"Saved to {hm.rel_to_home(Path(result['path']))} ({_sync_note(sync)}).",
    )


def tool_curate(args: Dict[str, Any]) -> str:
    result = hm.curate(str(args.get("note") or ""), str(args.get("category") or "other"), args.get("source"))
    if not result.get("saved"):
        return L("Такой факт в MEMORY.md уже есть — не дублирую.", "MEMORY.md already has this fact — not duplicating it.")
    sync = hm.try_sync(only=[hm.MEMORY_FILE], max_seconds=45)
    text = L(
        f"Добавлено в главную память MEMORY.md ({_sync_note(sync)}).",
        f"Added to the main memory MEMORY.md ({_sync_note(sync)}).",
    )
    if result.get("warning"):
        text += L(f"\nВнимание: {result['warning']}", f"\nNote: {result['warning']}")
    return text


def tool_forget(args: Dict[str, Any]) -> str:
    result = hm.forget(str(args.get("text") or ""), str(args.get("scope") or "curated"), bool(args.get("all_matches")))
    if result.get("removed"):
        return L(
            f"Удалено пунктов: {result['removed']} ({', '.join(result['files'])}). Копия — в {result['trash']}.",
            f"Items deleted: {result['removed']} ({', '.join(result['files'])}). A copy is in {result['trash']}.",
        )
    lines = [L(f"Ничего не удалено: {result.get('message')}.", f"Nothing deleted: {result.get('message')}.")]
    for candidate in result.get("candidates", []):
        lines.append(f"  - {candidate}")
    return "\n".join(lines)


def tool_status(args: Dict[str, Any]) -> str:
    return hm.format_status(hm.status())


def tool_sync(args: Dict[str, Any]) -> str:
    result = hm.try_sync(max_seconds=150)
    if result.get("busy"):
        return L("Индекс уже обновляется фоновой задачей.", "The index is already being updated by a background task.")
    if not result.get("ok"):
        return L(f"Не удалось обновить индекс: {result.get('error')}", f"Could not update the index: {result.get('error')}")
    text = L(
        f"Готово за {result['seconds']} с: файлов {result['files']}, обновлено {result['updated']}, "
        f"удалено {result['removed']}, фрагментов {result['chunks']}, векторов {result['embeddings']}.",
        f"Done in {result['seconds']} s: files {result['files']}, updated {result['updated']}, "
        f"removed {result['removed']}, fragments {result['chunks']}, vectors {result['embeddings']}.",
    )
    if not result.get("embeddings_complete"):
        text += L(" Векторы досчитаются фоновой задачей.", " A background task will compute the remaining vectors.")
    return text


HANDLERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "memory_search": tool_search,
    "memory_show": tool_show,
    "memory_remember": tool_remember,
    "memory_curate": tool_curate,
    "memory_forget": tool_forget,
    "memory_status": tool_status,
    "memory_sync": tool_sync,
}


def send(message: Dict[str, Any]) -> None:
    _PROTO_OUT.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    _PROTO_OUT.flush()


def respond(msg_id: Any, result: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    send(payload)


def handle(message: Dict[str, Any]) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    is_request = "id" in message and method is not None
    params = message.get("params") or {}
    if method is None:
        return  # ответ на наш запрос — мы их не шлём
    if not is_request:
        return  # уведомления (initialized, cancelled и т.п.) ответа не требуют
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        version = requested if re.match(r"^\d{4}-\d{2}-\d{2}$", requested) else FALLBACK_PROTOCOL
        respond(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": hm.SERVER_NAME, "title": L("Расширенная память", "Extended memory"), "version": hm.VERSION},
                "instructions": INSTRUCTIONS,
            },
        )
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            respond(msg_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
            return
        try:
            text = handler(params.get("arguments") or {})
            respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # ошибка инструмента — это результат, а не падение сервера
            traceback.print_exc(file=sys.stderr)
            respond(msg_id, {"content": [{"type": "text", "text": L(f"Ошибка: {exc}", f"Error: {exc}")}], "isError": True})
    elif method == "resources/list":
        respond(msg_id, {"resources": []})
    elif method == "resources/templates/list":
        respond(msg_id, {"resourceTemplates": []})
    elif method == "prompts/list":
        respond(msg_id, {"prompts": []})
    elif method == "logging/setLevel":
        respond(msg_id, {})
    else:
        respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        batch = message if isinstance(message, list) else [message]
        for item in batch:
            if isinstance(item, dict):
                try:
                    handle(item)
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    if "id" in item:
                        respond(item.get("id"), error={"code": -32603, "message": str(exc)})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
