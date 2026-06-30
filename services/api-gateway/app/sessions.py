"""Tiny in-memory anonymous session store for gateway smoke tests.

This is intentionally not a login system and not durable storage. It exists only to keep
short conversation context while testing the gateway/RAG path. Use Azure Table Storage,
Cosmos DB, Redis, or another shared store before relying on this with multiple replicas.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import settings

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or _now(), timezone.utc).isoformat()


def _valid_session_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if _SESSION_ID_RE.match(value) else None


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _prune_locked() -> None:
    now = _now()
    ttl = settings.SESSION_TTL_SECONDS
    expired = [sid for sid, item in _sessions.items() if now - float(item.get("updated_ts", 0)) > ttl]
    for sid in expired:
        _sessions.pop(sid, None)

    overflow = len(_sessions) - settings.SESSION_MAX_SESSIONS
    if overflow > 0:
        oldest = sorted(_sessions.items(), key=lambda kv: float(kv[1].get("updated_ts", 0)))[:overflow]
        for sid, _ in oldest:
            _sessions.pop(sid, None)


def create_session(session_id: str | None = None) -> dict[str, Any]:
    sid = _valid_session_id(session_id) or _new_session_id()
    now = _now()
    with _lock:
        _prune_locked()
        _sessions[sid] = {
            "session_id": sid,
            "created_ts": now,
            "updated_ts": now,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "turns": [],
        }
        return snapshot_locked(sid)


def ensure_session(session_id: str | None = None) -> dict[str, Any]:
    sid = _valid_session_id(session_id)
    with _lock:
        _prune_locked()
        if sid and sid in _sessions:
            item = _sessions[sid]
            item["updated_ts"] = _now()
            item["updated_at"] = _iso(item["updated_ts"])
            return snapshot_locked(sid)
    return create_session(sid)


def append_turn(session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
    sid = _valid_session_id(session_id) or _new_session_id()
    with _lock:
        _prune_locked()
        if sid not in _sessions:
            now = _now()
            _sessions[sid] = {
                "session_id": sid,
                "created_ts": now,
                "updated_ts": now,
                "created_at": _iso(now),
                "updated_at": _iso(now),
                "turns": [],
            }
        item = _sessions[sid]
        clean_turn = dict(turn)
        clean_turn.setdefault("ts", _iso())
        item["turns"].append(clean_turn)
        if len(item["turns"]) > settings.SESSION_MAX_TURNS:
            item["turns"] = item["turns"][-settings.SESSION_MAX_TURNS :]
        item["updated_ts"] = _now()
        item["updated_at"] = _iso(item["updated_ts"])
        return snapshot_locked(sid)


def snapshot_locked(session_id: str) -> dict[str, Any]:
    item = _sessions[session_id]
    return {
        "session_id": item["session_id"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "turn_count": len(item.get("turns", [])),
        "turns": list(item.get("turns", [])),
    }


def snapshot(session_id: str) -> dict[str, Any] | None:
    sid = _valid_session_id(session_id)
    if not sid:
        return None
    with _lock:
        _prune_locked()
        if sid not in _sessions:
            return None
        return snapshot_locked(sid)


def recent_llm_messages(session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
    sid = _valid_session_id(session_id)
    if not sid:
        return []
    limit = max_turns if max_turns is not None else settings.SESSION_CONTEXT_TURNS
    with _lock:
        _prune_locked()
        item = _sessions.get(sid)
        if not item:
            return []
        turns = item.get("turns", [])[-limit:]
    messages: list[dict[str, str]] = []
    for turn in turns:
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    return messages

