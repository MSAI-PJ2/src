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
    async def append_turn(self, session_id: str, turn: dict[str, Any], user_id: str | None = None) -> dict[str, Any]: ...
    async def snapshot(self, session_id: str) -> dict[str, Any] | None: ...               # 현재 상태 조회
    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]: ...
    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]: ...  # 이 사용자의 세션 목록(요약)
    async def rename(self, session_id: str, name: str | None,
                     user_id: str | None = None) -> dict[str, Any] | None: ...  # 표시 이름 설정/해제


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


# 세션 표시 이름: 사용자가 대화방에 붙이는 자유 라벨. 세션 ID(코드)가 그대로 노출되지
# 않도록 프론트가 이 이름을 보여준다(이름 없으면 preview→날짜 순으로 폴백). 프론트 표기
# 예: "승진 발표 상담 · 3f9c"(이름 + session_id 뒤 4자). 저장 전 한 줄로 정규화한다.
_SESSION_NAME_MAX = 60  # 목록 카드 한 줄에 들어가는 실용 상한

def clean_session_name(value: str | None) -> str | None:
    """표시 이름을 안전한 한 줄 문자열로 정리한다. 내용이 없으면 None(=이름 해제, 폴백으로 복귀).

    - 제어문자 제거 + 줄바꿈·연속 공백을 단일 공백으로 축약 (목록이 한 줄로 깨지지 않게)
    - 앞뒤 공백 제거 후 60자로 자름
    """
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value)      # 제어문자 → 공백
    collapsed = re.sub(r"\s+", " ", text).strip()        # 연속 공백/줄바꿈 → 단일 공백
    return collapsed[:_SESSION_NAME_MAX] or None


# 새 세션의 중립 초기 이름. 세션 내용(첫 발화 등)을 라벨로 노출하지 않으려는 의도 —
# 상담 도메인이라 민감할 수 있어, 이름은 사용자가 직접 붙이기 전까지 고정 중립값으로 둔다.
# 프론트는 여기에 session_id 뒤 4~6자를 붙여 구분한다(예: "새 대화 · 3f9c").
_DEFAULT_SESSION_NAME = "새 대화"

def default_session_name() -> str:
    """새 세션에 붙는 중립 초기 이름. (내용 기반 아님 — 민감정보 노출 방지)"""
    return _DEFAULT_SESSION_NAME


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
    return {"session_id": sid, "updated_ts": now, "created_at": iso(now), "updated_at": iso(now),
            "turns": [], "user_id": None, "name": default_session_name()}


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
            "turn_count": len(item["turns"]), "name": item.get("name"), "turns": list(item["turns"])}


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

    async def append_turn(self, session_id: str, turn: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
        sid = valid_session_id(session_id) or new_session_id()
        with _lock:
            _prune_locked()
            item = _sessions.setdefault(sid, _new_item(sid))
            if user_id and not item.get("user_id"):
                item["user_id"] = user_id  # 세션 최초 생성한 사용자로 한 번만 고정
            clean = dict(turn)
            clean.setdefault("ts", iso())
            if clean.get("role") == "user" and not item.get("preview"):
                item["preview"] = (clean.get("text") or "")[:40]  # 목록 카드용 첫 발화 미리보기
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
    
    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        with _lock:
            _prune_locked()
            return [
                {"session_id": sid, "created_at": it["created_at"], "updated_at": it["updated_at"],
                 "turn_count": len(it["turns"]), "preview": it.get("preview", ""),
                 "name": it.get("name")}
                for sid, it in _sessions.items() if it.get("user_id") == user_id
            ]

    async def rename(self, session_id: str, name: str | None,
                     user_id: str | None = None) -> dict[str, Any] | None:
        """세션 표시 이름을 설정/변경/해제(name=None 또는 빈 문자열)한다. 세션이 없으면 None.

        user_id 를 주면 소유자 확인: 다른 사용자의 세션이면 None(존재 자체를 숨기려 404 처리용).
        """
        sid = valid_session_id(session_id)
        with _lock:
            _prune_locked()
            item = _sessions.get(sid) if sid else None
            if item is None:
                return None
            if user_id and item.get("user_id") and item["user_id"] != user_id:
                return None
            # 빈 값으로 지우면 None 이 아니라 중립 기본값으로 복귀 — 항상 이름이 있게(코드 노출 방지)
            item["name"] = clean_session_name(name) or default_session_name()
            item["updated_ts"] = now_ts()
            item["updated_at"] = iso(item["updated_ts"])
            return _snapshot_locked(sid)


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
                "updated_at": item["updated_at"], "turn_count": len(turns),
                "name": item.get("name"), "turns": turns}

    def _new_doc(self, sid: str, user_id: str | None = None) -> dict[str, Any]:
        now = now_ts()
        item = {"id": sid, "session_id": sid, "user_id": user_id, "name": default_session_name(),
                "updated_ts": now, "created_at": iso(now), "updated_at": iso(now), "turns": []}
        if settings.SESSION_TTL_SECONDS > 0:
            item["ttl"] = settings.SESSION_TTL_SECONDS
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

    def _append_turn_sync(self, session_id: str, turn: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
        sid = valid_session_id(session_id) or new_session_id()
        clean = dict(turn)
        clean.setdefault("ts", iso())
        for _ in range(4):
            item = self._read(sid)
            if item is None:
                item = self._new_doc(sid, user_id)
                if clean.get("role") == "user" and not item.get("preview"):
                    item["preview"] = (clean.get("text") or "")[:40]  # 목록 카드용 첫 발화 미리보기
                item["turns"] = [clean]
                try:
                    self._container.create_item(body=item)
                    return self._to_snapshot(item)
                except self._conflict:
                    continue
            # 기존 문서에 턴 추가 (최대 개수 초과분은 앞에서부터 버림)
            if clean.get("role") == "user" and not item.get("preview"):
                item["preview"] = (clean.get("text") or "")[:40]
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

    def _rename_sync(self, session_id: str, name: str | None, user_id: str | None = None) -> dict[str, Any] | None:
        sid = valid_session_id(session_id)
        if not sid:
            return None
        cleaned = clean_session_name(name) or default_session_name()  # 빈 값 → 중립 기본값 복귀
        for _ in range(4):  # append 와 동일한 etag 조건부 쓰기 + 충돌 재시도
            item = self._read(sid)
            if item is None:
                return None
            if user_id and item.get("user_id") and item["user_id"] != user_id:
                return None  # 남의 세션 — 존재 숨김(None)
            item["name"] = cleaned
            self._touch(item)
            try:
                self._container.replace_item(item=sid, body=item, etag=item.get("_etag"),
                                             match_condition=self._if_not_modified)
                return self._to_snapshot(item)
            except self._precondition:
                continue  # 다른 요청이 먼저 씀(412) → 최신본 기준으로 재시도
        raise RuntimeError(f"cosmos session rename contention: {sid}")

    # --- SessionRepository 규격 (async) — 동기 구현을 스레드로 감싼 것 ---

    async def create(self, session_id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._create_sync, session_id)

    async def ensure(self, session_id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._ensure_sync, session_id)

    async def append_turn(self, session_id: str, turn: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._append_turn_sync, session_id, turn, user_id)

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        def _query():
            items = self._container.query_items(
                query="SELECT c.session_id, c.created_at, c.updated_at, c.preview, c.name, "
                      "ARRAY_LENGTH(c.turns) AS turn_count "
                      "FROM c WHERE c.user_id = @uid ORDER BY c.updated_at DESC",
                parameters=[{"name": "@uid", "value": user_id}],
                enable_cross_partition_query=True,
            )
            return list(items)
        return await asyncio.to_thread(_query)

    async def snapshot(self, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._snapshot_sync, session_id)

    async def rename(self, session_id: str, name: str | None,
                     user_id: str | None = None) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._rename_sync, session_id, name, user_id)

    async def recent_llm_messages(self, session_id: str, max_turns: int | None = None) -> list[dict[str, str]]:
        snap = await self.snapshot(session_id)
        if not snap:
            return []
        limit = max_turns if max_turns is not None else settings.SESSION_CONTEXT_TURNS
        return turns_to_llm_messages(snap.get("turns", [])[-limit:])


# ---------------------------------------------------------------------------
# 턴 빌더 — DB 에 저장하는 대화 기록 한 건의 형태.
# role = 발화 주체("user"/"assistant"), event = 어떤 상황의 기록인지.
# SSE 이벤트(respond/flow.py 구획 1)와는 별개의 "보관용" 형식. 필드를 바꾸면
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


def user_turn(text: str, primary: str, safety: dict, input_meta: dict, tts: dict | None,
              analysis: dict | None = None,
              selected_labels: list[dict] | None = None) -> dict:
    """사용자 발화 기록 — 분류 라벨과 안전검사 결과를 함께 저장한다.

    analysis: 분류가 어떻게 나왔는지의 관측 기록 (respond/flow.py 선행 필터·사다리 참조).
        context_merged=True 인 턴의 primary 는 "직전 맥락과 병합해 얻은" 라벨이므로,
        발화 단독 라벨이 필요한 소비자(재학습 데이터 추출, 라벨 분포 분석)는 이 필드로
        걸러야 한다 — API_CONTRACT.md §14 저장 문서 계약 참조.

    selected_labels: 멀티라벨 분류기가 "함께 선택한" 라벨 전체 [{label, score}, ...]
        (score 내림차순). 학습 데이터가 최대 4개 동시 라벨까지 포함하므로 primary 하나만
        저장하면 나머지 동시 왜곡 정보가 유실된다 — 왜곡 히스토리·집계·재학습 추출은
        이 필드를 쓴다. primary 는 라우팅 대표값일 뿐 분류 결과의 전부가 아니다.
    """
    turn = {"role": "user", "text": text, "primary": primary,
            "safety": "safe" if safety.get("safe") else "blocked",
            "safety_reason": safety.get("reason"), "input": input_meta, "tts": tts}
    if analysis is not None:
        turn["analysis"] = analysis
    if selected_labels is not None:
        turn["selected_labels"] = selected_labels
    return turn


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
