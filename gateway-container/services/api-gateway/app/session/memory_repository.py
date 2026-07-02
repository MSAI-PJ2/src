"""인메모리 세션 저장소 — 로컬 개발/스모크 테스트 전용.

레플리카 간 공유가 안 되고 재시작하면 사라진다. 운영은 SESSION_REPOSITORY=cosmos.
연산이 전부 프로세스 내 dict 조작이라 async 메서드에서 바로 처리한다
(스레드 오프로딩 불필요 — 잠금 유지 시간이 마이크로초 수준).
"""
from __future__ import annotations

import threading
from typing import Any

from ..core import settings
from .repository import iso, new_session_id, now_ts, turns_to_llm_messages, valid_session_id

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


def _new_item(session_id: str) -> dict[str, Any]:
    now = now_ts()
    return {
        "session_id": session_id,
        "created_ts": now,
        "updated_ts": now,
        "created_at": iso(now),
        "updated_at": iso(now),
        "turns": [],
    }


def _prune_locked() -> None:
    """TTL 초과·개수 초과 세션 제거(_lock 보유 상태에서 호출)."""
    now = now_ts()
    expired = [
        sid for sid, item in _sessions.items()
        if now - float(item.get("updated_ts", 0)) > settings.SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _sessions.pop(sid, None)

    overflow = len(_sessions) - settings.SESSION_MAX_SESSIONS
    if overflow > 0:
        oldest = sorted(_sessions.items(), key=lambda kv: float(kv[1].get("updated_ts", 0)))[:overflow]
        for sid, _ in oldest:
            _sessions.pop(sid, None)


def _snapshot_locked(session_id: str) -> dict[str, Any]:
    item = _sessions[session_id]
    return {
        "session_id": item["session_id"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "turn_count": len(item.get("turns", [])),
        "turns": list(item.get("turns", [])),
    }


class InMemorySessionRepository:
    async def create(self, session_id: str | None = None) -> dict[str, Any]:
        sid = valid_session_id(session_id) or new_session_id()
        with _lock:
            _prune_locked()
            _sessions[sid] = _new_item(sid)
            return _snapshot_locked(sid)

    async def ensure(self, session_id: str | None = None) -> dict[str, Any]:
        sid = valid_session_id(session_id)
        with _lock:
            _prune_locked()
            if sid and sid in _sessions:
                item = _sessions[sid]
                item["updated_ts"] = now_ts()
                item["updated_at"] = iso(item["updated_ts"])
                return _snapshot_locked(sid)
        return await self.create(sid)

    async def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        sid = valid_session_id(session_id) or new_session_id()
        with _lock:
            _prune_locked()
            if sid not in _sessions:
                _sessions[sid] = _new_item(sid)
            item = _sessions[sid]
            clean_turn = dict(turn)
            clean_turn.setdefault("ts", iso())
            item["turns"].append(clean_turn)
            if len(item["turns"]) > settings.SESSION_MAX_TURNS:
                item["turns"] = item["turns"][-settings.SESSION_MAX_TURNS:]
            item["updated_ts"] = now_ts()
            item["updated_at"] = iso(item["updated_ts"])
            return _snapshot_locked(sid)

    async def snapshot(self, session_id: str) -> dict[str, Any] | None:
        sid = valid_session_id(session_id)
        if not sid:
            return None
        with _lock:
            _prune_locked()
            if sid not in _sessions:
                return None
            return _snapshot_locked(sid)

    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
        sid = valid_session_id(session_id)
        if not sid:
            return []
        limit = max_turns if max_turns is not None else settings.SESSION_CONTEXT_TURNS
        with _lock:
            _prune_locked()
            item = _sessions.get(sid)
            if not item:
                return []
            turns = item.get("turns", [])[-limit:]
        return turns_to_llm_messages(turns)
