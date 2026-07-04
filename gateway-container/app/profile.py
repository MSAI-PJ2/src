"""[프로필] 사용자 프로필(설문·지역) 저장소 — memory/Cosmos 구현 + 설문 반영 규칙.

프로필 = 사용자 한 명의 설정 문서. 가구현 로그인(가상 ID — api/v1.py 구획 2)과 짝이다:
    {user_id, nickname, 시도, 시군구, location, emergency_contact, survey, privacy,
     survey_completed, created_at, updated_at}

왜 "시도/시군구" 한글 필드를 따로 두나:
    위기 분기의 지역 핫라인 조회(respond/policy.py resolve_region → _region_from_profile)가
    배포 DB(user_profiles)의 한글 필드를 읽도록 이미 설계돼 있다. 설문의
    location.sido/sigungu 를 저장할 때 이 두 필드로 "미러"해 두면, 설문만 저장돼도
    위기 시 지역 창구 안내가 그대로 살아난다 (지역 조회 코드는 수정 불필요).

저장 백엔드: 세션과 같은 스위치(SESSION_REPOSITORY)를 따른다 — 저장소 모드 노브 하나로 통일.
    memory(기본)  개발/테스트용. 서버 메모리에 저장, 재시작 시 소멸.
    cosmos        배포용. 컨테이너 USER_PROFILE_CONTAINER(비우면 user_profiles),
                  DB 는 USER_PROFILE_DATABASE(비우면 COSMOS_DATABASE), 파티션 키 /user_id.

메서드가 전부 "동기"인 이유 (세션 저장소와 다른 점):
    위기 경로의 지역 리졸버(policy.resolve_region)가 동기 함수라 여기서도 동기로 맞춘다.
    호출자가 이벤트루프를 막지 않도록 책임진다 — API 라우트(v1.py)와 위기 분기(flow.py)는
    asyncio.to_thread 로 감싸서 호출한다. (세션 문서 경합 같은 동시성 이슈는 프로필에선
    "본인 문서만 쓰기"라 발생 빈도가 낮아 etag 재시도 없이 단순 upsert 로 충분하다.)
"""
from __future__ import annotations

import os
import threading
from typing import Any, Protocol

from . import settings
from .session import iso, now_ts  # 시각 표기(UTC ISO)를 세션 문서와 동일하게

# 설문 페이로드에서 프로필 문서로 그대로 옮겨 담는 최상위 필드들.
# 프론트(app 레포 pages/4_설문.py)의 payload 모양과 1:1 — 필드가 늘면 여기에만 추가.
_SURVEY_FIELDS = ("nickname", "location", "emergency_contact", "survey", "privacy")


class ProfileRepository(Protocol):
    def get(self, user_id: str) -> dict[str, Any] | None: ...
    def ensure(self, user_id: str) -> dict[str, Any]: ...          # 없으면 만들고, 있으면 그대로
    def save_survey(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# 공용 헬퍼 — 문서 생성/설문 반영/외부 노출 형태
# ---------------------------------------------------------------------------

def _new_doc(user_id: str) -> dict[str, Any]:
    now = now_ts()
    return {"id": user_id, "user_id": user_id,          # id=user_id (Cosmos 문서 id 겸용)
            "survey_completed": False,
            "created_at": iso(now), "updated_at": iso(now)}


def _apply_survey(item: dict[str, Any], payload: dict[str, Any]) -> None:
    """설문 페이로드를 프로필 문서에 반영한다 (제자리 수정).

    - 최상위 필드는 온 것만 덮어쓴다 (부분 재제출 허용).
    - location.sido/sigungu 는 한글 필드 시도/시군구로 미러 → 위기 지역 조회가 읽는 계약.
    - survey_completed=True: 프론트(로그인 페이지)가 설문 완료 여부를 이 플래그로 판단한다.
    """
    for key in _SURVEY_FIELDS:
        if payload.get(key) is not None:
            item[key] = payload[key]
    location = payload.get("location") or {}
    if location.get("sido"):
        item["시도"] = location["sido"]
        item["시군구"] = location.get("sigungu") or None
    item["survey_completed"] = True
    item["updated_at"] = iso(now_ts())


def _to_public(item: dict[str, Any]) -> dict[str, Any]:
    """DB 문서 → API 응답 형태 (Cosmos 내부 필드 _rid/_etag/... 제거)."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# 인메모리 구현 — 개발/테스트용 (세션의 InMemorySessionRepository 와 같은 성격)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_profiles: dict[str, dict[str, Any]] = {}


class InMemoryProfileRepository:
    def get(self, user_id: str) -> dict[str, Any] | None:
        with _lock:
            item = _profiles.get(user_id)
            return _to_public(dict(item)) if item else None

    def ensure(self, user_id: str) -> dict[str, Any]:
        with _lock:
            item = _profiles.setdefault(user_id, _new_doc(user_id))
            return _to_public(dict(item))

    def save_survey(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            item = _profiles.setdefault(user_id, _new_doc(user_id))
            _apply_survey(item, payload)
            return _to_public(dict(item))


# ---------------------------------------------------------------------------
# Cosmos 구현 (배포). 문서 id = user_id, 파티션 키 = /user_id.
# 컨테이너는 인프라에서 미리 만든다 (세션과 동일한 원칙 — 코드가 DB 를 만들지 않는다).
# ---------------------------------------------------------------------------

class CosmosProfileRepository:
    def __init__(self) -> None:
        from azure.cosmos import CosmosClient
        from azure.cosmos import exceptions as cx

        self._not_found = cx.CosmosResourceNotFoundError

        conn = os.getenv("COSMOS_CONNECTION_STRING", "")
        if conn:
            client = CosmosClient.from_connection_string(conn)
        else:
            endpoint, key = os.getenv("COSMOS_ENDPOINT", ""), os.getenv("COSMOS_KEY", "")
            if not endpoint or not key:
                raise ValueError("cosmos requires COSMOS_ENDPOINT + COSMOS_KEY (or COSMOS_CONNECTION_STRING)")
            client = CosmosClient(endpoint, credential=key)

        database = settings.USER_PROFILE_DATABASE or os.getenv("COSMOS_DATABASE", "")
        if not database:
            raise ValueError("profile repository requires USER_PROFILE_DATABASE or COSMOS_DATABASE")
        container = settings.USER_PROFILE_CONTAINER or "user_profiles"
        self._container = client.get_database_client(database).get_container_client(container)

    def get(self, user_id: str) -> dict[str, Any] | None:
        try:
            return _to_public(self._container.read_item(item=user_id, partition_key=user_id))
        except self._not_found:
            return None

    def ensure(self, user_id: str) -> dict[str, Any]:
        existing = self.get(user_id)
        if existing:
            return existing
        item = _new_doc(user_id)
        self._container.upsert_item(body=item)
        return _to_public(item)

    def save_survey(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            item = self._container.read_item(item=user_id, partition_key=user_id)
        except self._not_found:
            item = _new_doc(user_id)   # 프로필 없이 설문부터 와도 받아준다 (견고성)
        _apply_survey(item, payload)
        self._container.upsert_item(body=item)
        return _to_public(item)


# ---------------------------------------------------------------------------
# 싱글톤 — 세션과 같은 노브(SESSION_REPOSITORY: memory | cosmos)로 백엔드 선택
# ---------------------------------------------------------------------------

def _build() -> ProfileRepository:
    backend = settings.SESSION_REPOSITORY.strip().lower()
    if backend in ("", "memory"):
        return InMemoryProfileRepository()
    if backend == "cosmos":
        return CosmosProfileRepository()
    raise ValueError("Unsupported SESSION_REPOSITORY: " + settings.SESSION_REPOSITORY)


profile_repository: ProfileRepository = _build()
