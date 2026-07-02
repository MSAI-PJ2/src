"""session — 세션 저장소(memory/Cosmos)와 세션 턴 빌더.

SESSION_REPOSITORY 환경변수로 백엔드를 고른다:
    memory  로컬/스모크 테스트용 인메모리 (기본)
    cosmos  Azure Cosmos DB NoSQL (운영)
"""
from ..core import settings
from .repository import SessionRepository


def _build_session_repository() -> SessionRepository:
    backend = settings.SESSION_REPOSITORY.strip().lower()
    if backend in ("", "memory", "inmemory", "in-memory"):
        from .memory_repository import InMemorySessionRepository

        return InMemorySessionRepository()
    if backend in ("cosmos", "cosmosdb", "azure_cosmos"):
        from .cosmos_repository import CosmosSessionRepository

        return CosmosSessionRepository()
    raise ValueError("Unsupported SESSION_REPOSITORY value: " + settings.SESSION_REPOSITORY)


session_repository: SessionRepository = _build_session_repository()

__all__ = ["SessionRepository", "session_repository"]
