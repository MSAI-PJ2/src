"""프로필 + 가구현 로그인(가상 ID) 계약 테스트.

가상 ID = 프론트가 발급·보관해 매 요청 x-user-id 헤더로 보내는 식별자 (api/v1.py 구획 2).
프로필 저장 규칙(한글 지역 미러 포함)은 app/profile.py, 위기 지역 연계는
respond/policy.py resolve_region — 여기서 "설문 저장 → 위기 시 지역 안내" 전 구간을 고정한다.
"""
import pytest

from test_v1_contract import (  # noqa: F401  (gateway 는 pytest 픽스처로 재사용)
    FakeSafety, _enable_entra, gateway, sse_events,
)

SURVEY = {
    "nickname": "테스트",
    "location": {"sido": "부산광역시", "sigungu": "해운대구"},
    "survey": {"prior_counseling": "없음"},
    "privacy": {"agreed_terms": True, "agreed_sensitive_profile": True},
}


@pytest.fixture(autouse=True)
def _clean_profiles():
    """인메모리 프로필 저장소를 테스트마다 비운다 (테스트 간 오염 방지)."""
    from app import profile
    profile._profiles.clear()
    yield
    profile._profiles.clear()


def _h(uid: str) -> dict:
    return {"x-user-id": uid}


# ── 프로필 생명주기 (가상 ID 기준) ──────────────────────────────────────────

def test_profile_lifecycle_with_virtual_id(gateway):
    client, _ = gateway
    # 처음엔 없다 → 404 (프론트 로그인 페이지가 이 404 를 보고 생성으로 넘어간다)
    assert client.get("/v1/profile", headers=_h("u-1")).status_code == 404
    # 생성 → 같은 ID 로 조회 가능
    created = client.post("/v1/profile", headers=_h("u-1")).json()
    assert created["user_id"] == "u-1" and created["survey_completed"] is False
    assert client.get("/v1/profile", headers=_h("u-1")).status_code == 200
    # 다른 가상 ID 에서는 안 보인다 (사용자별 격리)
    assert client.get("/v1/profile", headers=_h("u-2")).status_code == 404
    # 생성은 멱등 — 다시 눌러도 기존 프로필 그대로
    again = client.post("/v1/profile", headers=_h("u-1")).json()
    assert again["created_at"] == created["created_at"]


def test_survey_saves_completes_and_mirrors_region(gateway):
    client, _ = gateway
    client.post("/v1/profile", headers=_h("u-3"))
    saved = client.post("/v1/profile/survey", json=SURVEY, headers=_h("u-3")).json()
    # 완료 플래그 (프론트 로그인 페이지가 설문 완료 여부를 이 값으로 판단)
    assert saved["survey_completed"] is True
    assert saved["nickname"] == "테스트"
    # 한글 지역 미러 — 위기 지역 조회(resolve_region)가 읽는 계약
    assert saved["시도"] == "부산광역시" and saved["시군구"] == "해운대구"
    # 재조회해도 동일 + 프로필 재생성(ensure)이 설문 완료를 되돌리지 않는다
    assert client.get("/v1/profile", headers=_h("u-3")).json()["survey_completed"] is True
    assert client.post("/v1/profile", headers=_h("u-3")).json()["survey_completed"] is True


def test_survey_without_prior_create_still_works(gateway):
    """프로필 생성 없이 설문부터 와도 받아준다 (견고성 — 프론트 순서 꼬임 대비)."""
    client, _ = gateway
    saved = client.post("/v1/profile/survey", json=SURVEY, headers=_h("u-4")).json()
    assert saved["user_id"] == "u-4" and saved["survey_completed"] is True


# ── 가상 ID 규칙 ────────────────────────────────────────────────────────────

def test_invalid_virtual_id_rejected(gateway):
    client, _ = gateway
    # 공백·특수문자·과길이 — 형식 위반은 조용히 anonymous 로 흘리지 않고 400 으로 거절
    assert client.get("/v1/profile", headers=_h("bad id!")).status_code == 400
    assert client.get("/v1/profile", headers=_h("x" * 65)).status_code == 400


def test_no_header_falls_back_to_anonymous(gateway):
    """헤더가 없으면 기존과 동일하게 anonymous — 이전 프론트 하위 호환."""
    client, _ = gateway
    assert client.post("/v1/profile").json()["user_id"] == "anonymous"


def test_entra_mode_ignores_virtual_id(gateway, monkeypatch):
    """entra 모드에서는 JWT 의 oid 가 user_id — x-user-id 헤더로 남의 계정 접근 불가."""
    client, _ = gateway
    _enable_entra(monkeypatch)
    created = client.post("/v1/profile",
                          headers={"Authorization": "Bearer good-token", "x-user-id": "u-evil"})
    assert created.json()["user_id"] == "user-abc-123"   # oid 가 이긴다


# ── 설문 지역 → 위기 지역 안내 (end-to-end 배선 검증) ───────────────────────

def _arm_hotline_capture(monkeypatch):
    """지역 연락처 DB 를 켜고 조회 호출을 캡처한다 (test_v1_contract 위기 테스트와 동일 패턴)."""
    from app import settings
    from app.respond import policy
    monkeypatch.setattr(settings, "HOTLINE_CONTAINER", "kfsp_centers")
    calls = []
    monkeypatch.setattr(policy, "lookup_regional_hotlines",
                        lambda region, district=None: calls.append((region, district)) or [])
    return calls


def test_crisis_uses_profile_region(gateway, monkeypatch):
    """설문에 저장한 지역이, 위기 발화 시 metadata.region 없이도 지역 조회에 쓰인다."""
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    calls = _arm_hotline_capture(monkeypatch)

    client.post("/v1/profile/survey", json=SURVEY, headers=_h("u-crisis"))
    sse_events(client.post("/v1/respond", json={"text": "더 살 이유가 없는 것 같아요"},
                           headers=_h("u-crisis")))
    assert calls == [("부산광역시", "해운대구")]   # 프로필 지역으로 조회됨


def test_metadata_region_still_beats_profile(gateway, monkeypatch):
    """프론트가 지역을 명시(metadata.region)하면 프로필보다 우선한다 (기존 우선순위 유지)."""
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    calls = _arm_hotline_capture(monkeypatch)

    client.post("/v1/profile/survey", json=SURVEY, headers=_h("u-pref"))
    sse_events(client.post("/v1/respond",
                           json={"text": "더 살 이유가 없는 것 같아요",
                                 "metadata": {"region": "서울특별시"}},
                           headers=_h("u-pref")))
    assert calls == [("서울특별시", None)]


def test_anonymous_never_reads_profile_region(gateway, monkeypatch):
    """anonymous(가상 ID 미전송)는 공유 계정 — 프로필 지역을 쓰지 않는다 (타인 지역 노출 방지)."""
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    calls = _arm_hotline_capture(monkeypatch)

    client.post("/v1/profile/survey", json=SURVEY)          # anonymous 로 설문 저장
    sse_events(client.post("/v1/respond", json={"text": "더 살 이유가 없는 것 같아요"}))
    assert calls == []   # region 이 없으니 지역 조회 자체를 안 한다 (전국 공통만)
