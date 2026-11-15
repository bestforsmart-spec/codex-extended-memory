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

# stdout принадлежит протоколу: любой случайный print уходит в stderr
hm.fix_stdio()
_PROTO_OUT = sys.stdout.buffer
sys.stdout = sys.stderr

FALLBACK_PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "Долговременная память пользователя между сессиями. В начале нетривиальной задачи ищи прошлый контекст "
    "через memory_search (короткий запрос из ключевых слов), подробности — memory_show. Долговечные итоги "
    "(решения и почему, состояние проектов, предпочтения) сохраняй через memory_remember, самое важное и "
    "устойчивое — через memory_curate. Секреты не сохраняй никогда."
)

TOOLS = [
    {
        "name": "memory_search",
        "title": "Поиск по памяти",
        "description": (
            "Ищет в долговременной памяти: главная память MEMORY.md, дневники, история прошлых сессий, "
            "подключённые папки с заметками. Гибридный поиск по словам и по смыслу. Запрос — несколько "
            "ключевых слов (проект, сервис, клиент, ошибка, решение)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Короткий запрос из ключевых слов."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_show",
        "title": "Открыть фрагмент памяти",
        "description": "Показывает строки файла памяти по пути и номерам строк из результата memory_search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь из результата поиска, например memory/2026-09-25.md"},
                "start": {"type": "integer", "minimum": 1, "default": 1},
                "end": {"type": "integer", "minimum": 1, "description": "По умолчанию start+80, максимум start+400."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_remember",
        "title": "Запомнить (дневник)",
        "description": (
            "Записывает короткий долговечный факт в дневник памяти за сегодня: принятое решение и почему, "
            "что сделано, состояние проекта, следующий шаг, предпочтение пользователя. Не для секретов и шума."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "maxLength": 4000, "description": "Факт одной-тремя фразами. Без секретов."},
                "source": {"type": "string", "maxLength": 300, "description": "Необязательно: проект или контекст."},
            },
            "required": ["note"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_curate",
        "title": "Запомнить в главную память",
        "description": (
            "Добавляет высокосигнальный факт в главную память MEMORY.md, которая видна в начале КАЖДОЙ сессии: "
            "устойчивые правила работы, долгосрочные предпочтения, ключевые факты о проектах и людях, важные "
            "решения. Держи коротким; сначала проверь memory_search, нет ли уже такого."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "maxLength": 2000, "description": "Один факт одной строкой. Без секретов."},
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
        "title": "Забыть",
        "description": (
            "Удаляет устаревший или ошибочный пункт памяти по фрагменту его текста (копия уходит в "
            ".state/forgotten.md). По умолчанию только в MEMORY.md; scope=all — ещё и в дневниках."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 4, "description": "Точный фрагмент текста пункта."},
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
        "title": "Состояние памяти",
        "description": "Показывает состояние индекса памяти: сколько файлов и фрагментов, работает ли смысловой поиск.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "memory_sync",
        "title": "Обновить индекс памяти",
        "description": "Переиндексирует память (нужно редко: индекс обновляется сам после записи и раз в полчаса).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _sync_note(result: Dict[str, Any]) -> str:
    if result.get("busy"):
        return "индекс обновляется фоновой задачей"
    if not result.get("ok"):
        return f"индекс обновится позже ({result.get('error')})"
    if result.get("embedding_error"):
        return "проиндексировано по словам; смысловой индекс догонит позже"
    return "проиндексировано"


def tool_search(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("пустой запрос")
    return hm.format_search(hm.search(query, int(args.get("limit") or 8)))


def tool_show(args: Dict[str, Any]) -> str:
    start = int(args.get("start") or 1)
    end = args.get("end")
    return hm.show(str(args.get("path") or ""), start, int(end) if end else None)


def tool_remember(args: Dict[str, Any]) -> str:
    result = hm.remember(str(args.get("note") or ""), args.get("source"))
    if not result.get("saved"):
        return "Такой пункт в сегодняшнем дневнике уже есть — не дублирую."
    sync = hm.try_sync(only=[Path(result["path"])], max_seconds=45)
    return f"Сохранено в {hm.rel_to_home(Path(result['path']))} ({_sync_note(sync)})."


def tool_curate(args: Dict[str, Any]) -> str:
    result = hm.curate(str(args.get("note") or ""), str(args.get("category") or "other"), args.get("source"))
    if not result.get("saved"):
        return "Такой факт в MEMORY.md уже есть — не дублирую."
    sync = hm.try_sync(only=[hm.MEMORY_FILE], max_seconds=45)
    text = f"Добавлено в главную память MEMORY.md ({_sync_note(sync)})."
    if result.get("warning"):
        text += f"\nВнимание: {result['warning']}"
    return text


def tool_forget(args: Dict[str, Any]) -> str:
    result = hm.forget(str(args.get("text") or ""), str(args.get("scope") or "curated"), bool(args.get("all_matches")))
    if result.get("removed"):
        return f"Удалено пунктов: {result['removed']} ({', '.join(result['files'])}). Копия — в {result['trash']}."
    lines = [f"Ничего не удалено: {result.get('message')}."]
    for candidate in result.get("candidates", []):
        lines.append(f"  - {candidate}")
    return "\n".join(lines)


def tool_status(args: Dict[str, Any]) -> str:
    return hm.format_status(hm.status())


def tool_sync(args: Dict[str, Any]) -> str:
    result = hm.try_sync(max_seconds=150)
    if result.get("busy"):
        return "Индекс уже обновляется фоновой задачей."
    if not result.get("ok"):
        return f"Не удалось обновить индекс: {result.get('error')}"
    text = (
        f"Готово за {result['seconds']} с: файлов {result['files']}, обновлено {result['updated']}, "
        f"удалено {result['removed']}, фрагментов {result['chunks']}, векторов {result['embeddings']}."
    )
    if not result.get("embeddings_complete"):
        text += " Векторы досчитаются фоновой задачей."
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
                "serverInfo": {"name": hm.SERVER_NAME, "title": "Расширенная память", "version": hm.VERSION},
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
            respond(msg_id, {"content": [{"type": "text", "text": f"Ошибка: {exc}"}], "isError": True})
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
