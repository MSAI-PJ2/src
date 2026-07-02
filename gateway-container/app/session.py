"""[세션] 대화 기록의 모든 것 — 저장소 규격 + memory/Cosmos 구현 + 턴 빌더.

세션 = 대화방 하나. 턴 = 발화 하나(사용자 또는 AI). 세션 문서 형태:
    {session_id, created_at, updated_at, turns: [턴, 턴, ...]}

구성 (위에서 아래로):
    1. SessionRepository(Protocol)  저장소 규격 — 모든 메서드 async
    2. 공용 헬퍼                    시각/ID 검증/LLM 메시지 변환
    3. InMemorySessionRepository    개발/테스트용 (재시작 시 소멸)
    4. CosmosSessionRepository      운영용 (Azure Cosmos DB, etag 동시성 보호)
    5. 턴 빌더                      DB 에 저장하는 턴의 형태 (SSE 이벤트와 별개)
    6. session_repository 싱글톤    SESSION_REPOSITORY 환경변수로 구현 선택

Entra 로그인 도입 시(api/v1.py 구획 2 가이드) 세션 문서에 user_id 를 넣어
"내 세션만 접근"을 이 계층에서 보장한다.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from . import settings

# 허용하는 session_id 형식: 영문/숫자/일부 기호, 최대 128자 (이상한 값 저장 방지)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SessionRepository(Protocol):
    async def create(self, session_id: str | None = None) -> dict[str, Any]: ...
    async def ensure(self, session_id: str | None = None) -> dict[str, Any]: ...          # 없으면 만들고, 있으면 갱신
    async def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]: ...
    async def snapshot(self, session_id: str) -> dict[str, Any] | None: ...               # 현재 상태 조회
    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]: ...


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------

def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    """사람이 읽을 수 있는 시각 문자열 (UTC)."""
    return datetime.fromtimestamp(ts or now_ts(), timezone.utc).isoformat()


def valid_session_id(value: str | None) -> str | None:
    """형식에 맞는 session_id 만 통과시킨다. 아니면 None."""
    if not value:
        return None
    value = value.strip()
    return value if _SESSION_ID_RE.match(value) else None


def new_session_id() -> str:
    return str(uuid.uuid4())  # 전 세계적으로 겹치지 않는 무작위 ID


def turns_to_llm_messages(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    """저장된 턴들 → LLM 이 이해하는 대화 형식([{role, content}, ...])으로 변환.

    텍스트가 없는 턴(STT/OCR 실패 기록 등)은 role/text 조건에서 걸러진다.
    """
    return [{"role": t["role"], "content": (t.get("text") or "").strip()}
            for t in turns
            if t.get("role") in ("user", "assistant") and (t.get("text") or "").strip()]


# ---------------------------------------------------------------------------
# 인메모리 구현 — 서버 메모리(dict)에 저장. 재시작하면 사라지고 서버 간 공유 안 됨.
# 개발/테스트 전용이며, 운영은 CosmosSessionRepository.
# ---------------------------------------------------------------------------

_lock = threading.Lock()   # 여러 요청이 동시에 dict 를 고칠 때 꼬이지 않게 하는 잠금
_sessions: dict[str, dict[str, Any]] = {}


def _new_item(sid: str) -> dict[str, Any]:
    now = now_ts()
    return {"session_id": sid, "updated_ts": now,
            "created_at": iso(now), "updated_at": iso(now), "turns": []}


def _prune_locked() -> None:
    """유효시간(TTL)이 지난 세션을 정리한다 (_lock 을 잡은 상태에서만 호출)."""
    now = now_ts()
    for sid in [s for s, it in _sessions.items()
                if now - float(it.get("updated_ts", 0)) > settings.SESSION_TTL_SECONDS]:
        _sessions.pop(sid, None)


def _snapshot_locked(sid: str) -> dict[str, Any]:
    """저장된 원본이 아니라 복사본을 돌려준다 (밖에서 고쳐도 원본이 안 바뀌게)."""
    item = _sessions[sid]
    return {"session_id": sid, "created_at": item["created_at"], "updated_at": item["updated_at"],
            "turn_count": len(item["turns"]), "turns": list(item["turns"])}


class InMemorySessionRepository:
    async def create(self, session_id: str | None = None) -> dict[str, Any]:
        sid = valid_session_id(session_id) or new_session_id()
        with _lock:
            _prune_locked()
            _sessions[sid] = _new_item(sid)
            return _snapshot_locked(sid)

    async def ensure(self, session_id: str | None = None) -> dict[str, Any]:
        """세션이 있으면 갱신시각만 업데이트, 없으면 새로 만든다."""
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
        """턴 하나를 뒤에 붙인다. 최대 개수(SESSION_MAX_TURNS)를 넘으면 앞에서부터 버린다."""
        sid = valid_session_id(session_id) or new_session_id()
        with _lock:
            _prune_locked()
            item = _sessions.setdefault(sid, _new_item(sid))
            clean = dict(turn)
            clean.setdefault("ts", iso())  # 저장 시각 기록
            item["turns"] = (item["turns"] + [clean])[-settings.SESSION_MAX_TURNS:]
            item["updated_ts"] = now_ts()
            item["updated_at"] = iso(item["updated_ts"])
            return _snapshot_locked(sid)

    async def snapshot(self, session_id: str) -> dict[str, Any] | None:
        sid = valid_session_id(session_id)
        with _lock:
            _prune_locked()
            return _snapshot_locked(sid) if sid and sid in _sessions else None

    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
        """LLM 에 넘길 최근 대화 — 기본은 최근 SESSION_CONTEXT_TURNS(6)개 턴."""
        sid = valid_session_id(session_id)
        limit = max_turns if max_turns is not None else settings.SESSION_CONTEXT_TURNS
        with _lock:
            item = _sessions.get(sid) if sid else None
            turns = list(item["turns"][-limit:]) if item else []
        return turns_to_llm_messages(turns)


# ---------------------------------------------------------------------------
# Cosmos DB 구현 (운영). 컨테이너: 문서 id = session_id, 파티션 키 = /session_id.
# 컨테이너는 인프라에서 미리 만든다 — 코드가 임의로 DB 를 만들지 않아야
# 잘못된 계정에 쓰는 실수를 일찍 발견할 수 있다.
#
# 기술 배경 두 가지:
# - Cosmos SDK 는 동기(응답 대기 중 서버가 멈춤) → public 메서드는 to_thread 로 오프로딩.
# - 턴 추가는 "읽기→추가→쓰기" 3단계라 동시 요청이 겹치면 덮어쓸 수 있다 →
#   etag(문서 버전표)로 "내가 읽은 뒤 아무도 안 고쳤을 때만 쓰기"를 걸고 충돌 시 재시도.
# ---------------------------------------------------------------------------

class CosmosSessionRepository:
    def __init__(self) -> None:
        from azure.core import MatchConditions
        from azure.cosmos import CosmosClient
        from azure.cosmos import exceptions as cx

        # 예외 클래스들을 멤버로 보관 (아래 메서드들이 except 절에서 사용)
        self._not_found = cx.CosmosResourceNotFoundError          # 문서 없음
        self._conflict = cx.CosmosResourceExistsError             # 생성 시 이미 존재
        self._precondition = cx.CosmosAccessConditionFailedError  # etag 불일치(누가 먼저 씀)
        self._if_not_modified = MatchConditions.IfNotModified

        # 접속 정보: 연결 문자열 하나 또는 endpoint+key 조합
        conn = os.getenv("COSMOS_CONNECTION_STRING", "")
        if conn:
            client = CosmosClient.from_connection_string(conn)
        else:
            endpoint, key = os.getenv("COSMOS_ENDPOINT", ""), os.getenv("COSMOS_KEY", "")
            if not endpoint or not key:
                raise ValueError("cosmos requires COSMOS_ENDPOINT + COSMOS_KEY (or COSMOS_CONNECTION_STRING)")
            client = CosmosClient(endpoint, credential=key)

        database, container = os.getenv("COSMOS_DATABASE", ""), os.getenv("COSMOS_CONTAINER", "")
        if not database or not container:
            raise ValueError("cosmos requires COSMOS_DATABASE + COSMOS_CONTAINER")
        self._container = client.get_database_client(database).get_container_client(container)

    # --- 내부 동기 구현 (반드시 to_thread 를 통해서만 호출) ---

    def _read(self, sid: str) -> dict[str, Any] | None:
        """문서 하나 읽기. 없으면 None (예외를 밖으로 던지지 않는다)."""
        try:
            return self._container.read_item(item=sid, partition_key=sid)
        except self._not_found:
            return None

    @staticmethod
    def _to_snapshot(item: dict[str, Any]) -> dict[str, Any]:
        """DB 문서 → API 가 돌려주는 형태(snapshot)로 변환."""
        turns = list(item.get("turns") or [])
        return {"session_id": item["session_id"], "created_at": item["created_at"],
                "updated_at": item["updated_at"], "turn_count": len(turns), "turns": turns}

    def _new_doc(self, sid: str) -> dict[str, Any]:
        now = now_ts()
        item = {"id": sid, "session_id": sid, "updated_ts": now,
                "created_at": iso(now), "updated_at": iso(now), "turns": []}
        if settings.SESSION_TTL_SECONDS > 0:
            item["ttl"] = settings.SESSION_TTL_SECONDS  # Cosmos 가 TTL 후 자동 삭제
        return item

    def _touch(self, item: dict[str, Any]) -> None:
        """갱신 시각과 TTL 을 새로 찍는다 (문서를 쓰기 직전에 호출)."""
        item["updated_ts"] = now_ts()
        item["updated_at"] = iso(item["updated_ts"])
        if settings.SESSION_TTL_SECONDS > 0:
            item["ttl"] = settings.SESSION_TTL_SECONDS

    def _create_sync(self, session_id: str | None) -> dict[str, Any]:
        item = self._new_doc(valid_session_id(session_id) or new_session_id())
        self._container.upsert_item(body=item)  # upsert = 있으면 덮어쓰고 없으면 생성
        return self._to_snapshot(item)

    def _ensure_sync(self, session_id: str | None) -> dict[str, Any]:
        sid = valid_session_id(session_id)
        if not sid:
            return self._create_sync(None)
        item = self._read(sid)
        if item is None:
            return self._create_sync(sid)
        self._touch(item)
        try:
            # etag 조건부 쓰기: 내가 읽은 버전 그대로일 때만 교체
            self._container.replace_item(item=sid, body=item, etag=item.get("_etag"),
                                         match_condition=self._if_not_modified)
        except self._precondition:
            item = self._read(sid) or item  # 다른 요청이 먼저 갱신함 — 최신본을 반환만 한다
        return self._to_snapshot(item)

    def _append_turn_sync(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        sid = valid_session_id(session_id) or new_session_id()
        clean = dict(turn)
        clean.setdefault("ts", iso())
        # 최대 4회 재시도: 동시 요청과 충돌하면 최신 문서를 다시 읽어 다시 쓴다 → 턴 유실 방지
        for _ in range(4):
            item = self._read(sid)
            if item is None:
                # 문서가 아직 없음 → 새로 생성. 동시에 다른 요청이 먼저 만들었으면(409) 재시도
                item = self._new_doc(sid)
                item["turns"] = [clean]
                try:
                    self._container.create_item(body=item)
                    return self._to_snapshot(item)
                except self._conflict:
                    continue
            # 기존 문서에 턴 추가 (최대 개수 초과분은 앞에서부터 버림)
            item["turns"] = (list(item.get("turns") or []) + [clean])[-settings.SESSION_MAX_TURNS:]
            self._touch(item)
            try:
                self._container.replace_item(item=sid, body=item, etag=item.get("_etag"),
                                             match_condition=self._if_not_modified)
                return self._to_snapshot(item)
            except self._precondition:
                continue  # 다른 요청이 먼저 씀(412) → 최신본 기준으로 재시도
        raise RuntimeError(f"cosmos session write contention: {sid}")

    def _snapshot_sync(self, session_id: str) -> dict[str, Any] | None:
        sid = valid_session_id(session_id)
        if not sid:
            return None
        item = self._read(sid)
        return self._to_snapshot(item) if item else None

    # --- SessionRepository 규격 (async) — 동기 구현을 스레드로 감싼 것 ---

    async def create(self, session_id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._create_sync, session_id)

    async def ensure(self, session_id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._ensure_sync, session_id)

    async def append_turn(self, session_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._append_turn_sync, session_id, turn)

    async def snapshot(self, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._snapshot_sync, session_id)

    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
        snap = await self.snapshot(session_id)
        if not snap:
            return []
        limit = max_turns if max_turns is not None else settings.SESSION_CONTEXT_TURNS
        return turns_to_llm_messages(snap.get("turns", [])[-limit:])


# ---------------------------------------------------------------------------
# 턴 빌더 — DB 에 저장하는 대화 기록 한 건의 형태.
# role = 발화 주체("user"/"assistant"), event = 어떤 상황의 기록인지.
# SSE 이벤트(counsel/flow.py 구획 1)와는 별개의 "보관용" 형식. 필드를 바꾸면
# GET /v1/sessions 응답이 바뀌므로 API_CONTRACT.md 를 함께 갱신한다.
# ---------------------------------------------------------------------------

def stt_failed_turn(input_meta: dict, result: dict, tts: dict | None) -> dict:
    """음성 인식 실패 기록 — 어떤 오디오가 왜 실패했는지 남긴다."""
    return {"role": "user", "text": "", "event": "stt_failed",
            "input": input_meta, "stt_result": result, "tts": tts}


def ocr_failed_turn(input_meta: dict, result: dict, tts: dict | None) -> dict:
    """이미지 OCR 실패 기록 — 어떤 이미지가 왜 실패했는지 남긴다."""
    return {"role": "user", "text": "", "event": "ocr_failed",
            "input": input_meta, "ocr_result": result, "tts": tts}


def input_pending_turn(input_meta: dict, tts: dict | None) -> dict:
    """빈 입력 요청 기록."""
    return {"role": "user", "text": "", "event": "input_pending", "input": input_meta, "tts": tts}


def user_turn(text: str, primary: str, safety: dict, input_meta: dict, tts: dict | None) -> dict:
    """사용자 발화 기록 — 분류 라벨과 안전검사 결과를 함께 저장한다."""
    return {"role": "user", "text": text, "primary": primary,
            "safety": "safe" if safety.get("safe") else "blocked",
            "safety_reason": safety.get("reason"), "input": input_meta, "tts": tts}


def crisis_turn(payload: dict) -> dict:
    """위기 분기 기록 — AI 답변 대신 고정 위기 메시지가 나갔다는 표시(blocked=True)."""
    return {"role": "assistant", "text": payload.get("message", ""), "event": "crisis",
            "blocked": True, "reason": payload.get("reason")}


def assistant_turn(text: str, primary: str, chunks: list[dict], policy: dict | None = None) -> dict:
    """AI 답변 기록 — 어떤 참고자료(rag_chunk_ids)와 정책(policy)으로 생성했는지 남긴다."""
    turn = {"role": "assistant", "text": text, "event": "respond", "primary": primary,
            "rag_chunk_ids": [c["id"] for c in chunks]}
    if policy:
        turn["policy"] = policy  # 적용된 컨텍스트 정책 (context_policy.py)
    return turn


# ---------------------------------------------------------------------------
# 싱글톤 — SESSION_REPOSITORY: memory(개발/테스트, 기본) | cosmos(운영)
# ---------------------------------------------------------------------------

def _build() -> SessionRepository:
    backend = settings.SESSION_REPOSITORY.strip().lower()
    if backend in ("", "memory"):
        return InMemorySessionRepository()
    if backend == "cosmos":
        return CosmosSessionRepository()
    raise ValueError("Unsupported SESSION_REPOSITORY: " + settings.SESSION_REPOSITORY)


session_repository: SessionRepository = _build()
