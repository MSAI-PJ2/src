"""Session repository boundary for gateway orchestration.

The current implementation delegates to the existing in-memory sessions module.
Cosmos DB can replace this adapter later without changing dag.py/main.py call
sites or the external API contract.
"""
from __future__ import annotations

from typing import Any, Protocol

from .. import sessions


class SessionRepository(Protocol):
    def create(self, session_id: str | None = None) -> dict[str, Any]: ...

    def ensure(self, session_id: str | None = None) -> dict[str, Any]: ...

    def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]: ...

    def snapshot(self, session_id: str) -> dict[str, Any] | None: ...

    def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]: ...


class InMemorySessionRepository:
    """Adapter over app.sessions for current anonymous smoke-test sessions."""

    def create(self, session_id: str | None = None) -> dict[str, Any]:
        return sessions.create_session(session_id)

    def ensure(self, session_id: str | None = None) -> dict[str, Any]:
        return sessions.ensure_session(session_id)

    def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        return sessions.append_turn(session_id, turn)

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        return sessions.snapshot(session_id)

    def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
        return sessions.recent_llm_messages(session_id, max_turns)


session_repository: SessionRepository = InMemorySessionRepository()
