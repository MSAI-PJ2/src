"""Session repository boundary for gateway orchestration.

SESSION_REPOSITORY=memory keeps the existing local/in-memory behavior.
SESSION_REPOSITORY=cosmos stores anonymous session turns in Azure Cosmos DB
without changing dag.py/main.py call sites or the external API contract.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from .. import sessions, settings

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


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


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


class CosmosSessionRepository:
    """Azure Cosmos DB backed session repository.

    Expected NoSQL container shape:
    - item id: session_id
    - partition key: /session_id

    The container should be created by Azure/portal/infra before app startup.
    This adapter intentionally does not create databases or containers at
    runtime, so accidental writes to the wrong account are easier to catch.
    """

    def __init__(self) -> None:
        try:
            from azure.cosmos import CosmosClient
            from azure.cosmos.exceptions import CosmosResourceNotFoundError
        except ImportError as exc:  # pragma: no cover - only hit when dependency is missing.
            raise RuntimeError("SESSION_REPOSITORY=cosmos requires azure-cosmos package") from exc

        self._not_found_error = CosmosResourceNotFoundError
        connection_string = _env_first("COSMOS_CONNECTION_STRING", "AZURE_COSMOS_CONNECTION_STRING")
        endpoint = _env_first("COSMOS_ENDPOINT", "COSMOS_SESSION_ENDPOINT", "AZURE_COSMOS_ENDPOINT")
        key = _env_first("COSMOS_KEY", "COSMOS_SESSION_KEY", "AZURE_COSMOS_KEY")
        database_name = _env_first("COSMOS_DATABASE", "COSMOS_SESSION_DATABASE", "AZURE_COSMOS_DATABASE")
        container_name = _env_first("COSMOS_CONTAINER", "COSMOS_SESSION_CONTAINER", "AZURE_COSMOS_CONTAINER")

        if connection_string:
            client = CosmosClient.from_connection_string(connection_string)
        else:
            missing = [
                name
                for name, value in {
                    "COSMOS_ENDPOINT(or COSMOS_SESSION_ENDPOINT)": endpoint,
                    "COSMOS_KEY(or COSMOS_SESSION_KEY)": key,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError("Cosmos session repository missing env vars: " + ", ".join(missing))
            client = CosmosClient(endpoint, credential=key)

        missing_store = [
            name
            for name, value in {
                "COSMOS_DATABASE(or COSMOS_SESSION_DATABASE)": database_name,
                "COSMOS_CONTAINER(or COSMOS_SESSION_CONTAINER)": container_name,
            }.items()
            if not value
        ]
        if missing_store:
            raise ValueError("Cosmos session repository missing env vars: " + ", ".join(missing_store))

        self._container = client.get_database_client(database_name).get_container_client(container_name)

    def _read(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self._container.read_item(item=session_id, partition_key=session_id)
        except self._not_found_error:
            return None

    @staticmethod
    def _to_snapshot(item: dict[str, Any]) -> dict[str, Any]:
        turns = list(item.get("turns") or [])
        return {
            "session_id": item["session_id"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "turn_count": len(turns),
            "turns": turns,
        }

    @staticmethod
    def _new_item(session_id: str) -> dict[str, Any]:
        now = _now()
        item: dict[str, Any] = {
            "id": session_id,
            "session_id": session_id,
            "created_ts": now,
            "updated_ts": now,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "turns": [],
        }
        if settings.SESSION_TTL_SECONDS > 0:
            item["ttl"] = settings.SESSION_TTL_SECONDS
        return item

    def create(self, session_id: str | None = None) -> dict[str, Any]:
        sid = _valid_session_id(session_id) or _new_session_id()
        item = self._new_item(sid)
        self._container.upsert_item(body=item)
        return self._to_snapshot(item)

    def ensure(self, session_id: str | None = None) -> dict[str, Any]:
        sid = _valid_session_id(session_id)
        if not sid:
            return self.create(None)
        item = self._read(sid)
        if item is None:
            return self.create(sid)
        now = _now()
        item["updated_ts"] = now
        item["updated_at"] = _iso(now)
        if settings.SESSION_TTL_SECONDS > 0:
            item["ttl"] = settings.SESSION_TTL_SECONDS
        self._container.upsert_item(body=item)
        return self._to_snapshot(item)

    def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        sid = _valid_session_id(session_id) or _new_session_id()
        item = self._read(sid) or self._new_item(sid)
        clean_turn = dict(turn)
        clean_turn.setdefault("ts", _iso())
        turns = list(item.get("turns") or [])
        turns.append(clean_turn)
        if len(turns) > settings.SESSION_MAX_TURNS:
            turns = turns[-settings.SESSION_MAX_TURNS :]
        now = _now()
        item["turns"] = turns
        item["updated_ts"] = now
        item["updated_at"] = _iso(now)
        if settings.SESSION_TTL_SECONDS > 0:
            item["ttl"] = settings.SESSION_TTL_SECONDS
        self._container.upsert_item(body=item)
        return self._to_snapshot(item)

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        sid = _valid_session_id(session_id)
        if not sid:
            return None
        item = self._read(sid)
        return self._to_snapshot(item) if item else None

    def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
        snapshot = self.snapshot(session_id)
        if not snapshot:
            return []
        limit = max_turns if max_turns is not None else settings.SESSION_CONTEXT_TURNS
        turns = snapshot.get("turns", [])[-limit:]
        messages: list[dict[str, str]] = []
        for turn in turns:
            role = turn.get("role")
            text = (turn.get("text") or "").strip()
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": text})
        return messages


def _build_session_repository() -> SessionRepository:
    backend = settings.SESSION_REPOSITORY.strip().lower()
    if backend in ("", "memory", "inmemory", "in-memory"):
        return InMemorySessionRepository()
    if backend in ("cosmos", "cosmosdb", "azure_cosmos"):
        return CosmosSessionRepository()
    raise ValueError("Unsupported SESSION_REPOSITORY value: " + settings.SESSION_REPOSITORY)


session_repository: SessionRepository = _build_session_repository()
