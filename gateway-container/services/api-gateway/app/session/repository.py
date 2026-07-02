"""세션 저장소 공통 계약(Protocol)과 공용 헬퍼.

모든 메서드는 async 다 — Cosmos 같은 네트워크 백엔드가 이벤트루프를 막지 않도록
블로킹 SDK 호출은 각 구현에서 스레드로 오프로딩한다(cosmos_repository 참고).

Entra External ID 도입 시(core/auth.py 가이드) 세션 문서에 user_id 를 추가해
"본인 세션만 접근"을 이 계층에서 보장하는 것을 권장한다.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SessionRepository(Protocol):
    async def create(self, session_id: str | None = None) -> dict[str, Any]: ...

    async def ensure(self, session_id: str | None = None) -> dict[str, Any]: ...

    async def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]: ...

    async def snapshot(self, session_id: str) -> dict[str, Any] | None: ...

    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]: ...


def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), timezone.utc).isoformat()


def valid_session_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if _SESSION_ID_RE.match(value) else None


def new_session_id() -> str:
    return str(uuid.uuid4())


def turns_to_llm_messages(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    """저장된 턴 목록을 LLM 대화 히스토리(role/content)로 변환한다."""
    messages: list[dict[str, str]] = []
    for turn in turns:
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    return messages
