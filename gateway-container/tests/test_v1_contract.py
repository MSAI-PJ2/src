"""/v1 API 계약 테스트 — 프론트가 의존하는 SSE 이벤트 계약이 깨지지 않았음을 증명.

외부 서비스(cogdist·Content Safety·AI Search·Azure OpenAI·Speech)는 전부 가짜
어댑터로 대체하므로 네트워크·키 없이 로컬 실행된다. 기준 문서: API_CONTRACT.md.
"""
import json

import pytest
from fastapi.testclient import TestClient

CLS_RESULT = {
    "text": "테스트 발화", "mode": "single", "model": "cogdist-test", "model_version": "test",
    "threshold": 0.5, "primary": "흑백 사고",
    "labels": [{"label": "흑백 사고", "score": 0.91, "selected": True}],
}
CANDIDATES = [
    {"id": "d1", "content": "근거 검토 기법", "score": 0.9, "metadata": {"distortions": ["흑백 사고"]}},
    {"id": "d2", "content": "탈파국화 기법", "score": 0.5, "metadata": {}},
]
LLM_TOKENS = ["괜찮아요, ", "함께 살펴봐요."]


class FakeClassifier:
    async def classify_one(self, text, threshold=None):
        return {**CLS_RESULT, "text": text}

    async def classify_batch(self, texts, threshold=None):
        return {"results": [{"index": i, "ok": True, "result": {**CLS_RESULT, "text": t}, "error": None}
                            for i, t in enumerate(texts)]}


class FakeSafety:
    def __init__(self, safe=True):
        self.safe = safe

    async def check(self, text):
        if self.safe:
            return {"safe": True, "reason": None, "source": "fake"}
        return {"safe": False, "reason": "self_harm_signal", "source": "fake"}


class FakeRetriever:
    async def retrieve(self, text):
        return [dict(c) for c in CANDIDATES]


class FakeLlm:
    async def chat_stream_async(self, messages, options=None):
        for tok in LLM_TOKENS:
            yield tok


class FakeSpeech:
    def __init__(self, stt_ok=True):
        self.stt_ok = stt_ok

    async def transcribe_audio(self, audio):
        if self.stt_ok:
            return {"provider": "azure", "language": "ko-KR", "status": "completed",
                    "transcript": "음성으로 말한 문장", "confidence": None,
                    "recognition_status": "RecognizedSpeech"}
        return {"provider": "azure", "language": "ko-KR", "status": "no_match",
                "transcript": "", "recognition_status": "NoMatch", "reason": "NoMatch"}

    async def synthesize_tts(self, text, tts_options):
        return {"status": "completed", "provider": "azure", "text": text,
                "mime_type": "audio/wav", "format": "wav",
                "audio": {"kind": "base64", "data": "QUJD", "mime_type": "audio/wav"}}


@pytest.fixture()
def gateway(monkeypatch):
    """가짜 어댑터가 주입된 TestClient."""
    from app.services import services
    monkeypatch.setattr(services, "classifier", FakeClassifier())
    monkeypatch.setattr(services, "safety", FakeSafety(safe=True))
    monkeypatch.setattr(services, "retriever", FakeRetriever())
    monkeypatch.setattr(services, "llm", FakeLlm())
    monkeypatch.setattr(services, "speech", FakeSpeech(stt_ok=True))

    from app.main import app
    return TestClient(app), services


def sse_events(response) -> list[dict]:
    return [json.loads(f[len("data: "):]) for f in response.text.split("\n\n")
            if f.strip().startswith("data: ")]


def types_of(events) -> list[str]:
    return [e["type"] for e in events]


def test_healthz(gateway):
    client, _ = gateway
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_respond_text_event_sequence(gateway):
    client, _ = gateway
    r = client.post("/v1/respond", json={"text": "사람들 앞에 서면 다 망칠 것 같아요"})
    assert r.status_code == 200
    events = sse_events(r)
    assert types_of(events) == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]

    meta = events[0]
    assert meta["primary"] == CLS_RESULT["primary"]
    assert meta["labels"] == CLS_RESULT["labels"]
    assert meta["turn_count"] >= 1
    assert meta["input"]["input_type"] == "text"
    assert all(set(c) == {"id", "content"} for c in events[1]["chunks"])
    assert "".join(e["text"] for e in events if e["type"] == "token") == "".join(LLM_TOKENS)
    assert all(e["session_id"] == meta["session_id"] for e in events)


def test_respond_crisis_branch(gateway, monkeypatch):
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    events = sse_events(client.post("/v1/respond", json={"text": "더 살 이유가 없는 것 같아요"}))
    assert types_of(events) == ["meta", "crisis", "done"]
    crisis = events[1]
    assert crisis["blocked"] is True and crisis["message"]
    assert crisis["resources"] and all({"name", "phone"} <= set(r) for r in crisis["resources"])


def test_crisis_regional_hotlines(gateway, monkeypatch):
    """지역 연락처 DB 가 켜져 있고 metadata.region 이 오면 지역 창구가 전국 공통 앞에 붙는다."""
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    from app import settings
    from app.respond import policy
    monkeypatch.setattr(settings, "HOTLINE_CONTAINER", "kfsp_centers")
    regional = [{"name": "○○시자살예방센터", "phone": "033-000-0000", "address": "강원특별자치도 ○○시 ○○로 00"}]
    calls = []
    monkeypatch.setattr(policy, "lookup_regional_hotlines",
                        lambda region, district=None: calls.append((region, district)) or regional)

    events = sse_events(client.post("/v1/respond", json={
        "text": "더 살 이유가 없는 것 같아요",
        "metadata": {"region": "강원특별자치도"}}))
    crisis = events[1]
    assert crisis["resources"][:1] == regional            # 지역 창구가 맨 앞
    assert crisis["resources"][1:] == policy.HOTLINES     # 전국 공통이 그 뒤
    assert calls == [("강원특별자치도", None)]              # 시도만, 시군구(껍데기)는 None


def test_crisis_lookup_failure_never_blocks(gateway, monkeypatch):
    """지역 조회가 실패해도 위기 응답은 전국 공통 창구로 반드시 나간다."""
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    from app import settings
    from app.respond import policy
    monkeypatch.setattr(settings, "HOTLINE_CONTAINER", "kfsp_centers")

    def boom(region, district=None):
        raise RuntimeError("cosmos down")
    monkeypatch.setattr(policy, "lookup_regional_hotlines", boom)

    events = sse_events(client.post("/v1/respond", json={
        "text": "더 살 이유가 없는 것 같아요", "metadata": {"region": "강원특별자치도"}}))
    assert types_of(events) == ["meta", "crisis", "done"]
    assert events[1]["resources"] == policy.HOTLINES


def test_crisis_no_region_skips_lookup(gateway, monkeypatch):
    """DB 가 켜져 있어도 region 이 안 오면 조회 없이 전국 공통만 나간다."""
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))
    from app import settings
    from app.respond import policy
    monkeypatch.setattr(settings, "HOTLINE_CONTAINER", "kfsp_centers")
    calls = []
    monkeypatch.setattr(policy, "lookup_regional_hotlines",
                        lambda r, d=None: calls.append((r, d)) or [])

    events = sse_events(client.post("/v1/respond", json={"text": "더 살 이유가 없는 것 같아요"}))
    assert events[1]["resources"] == policy.HOTLINES
    assert calls == []   # region 없음 → 조회 자체를 안 한다


def test_hotline_kfsp_field_mapping(monkeypatch):
    """실제 조회 로직: kfsp_centers 한글 필드(기관명/전화/주소)를 영문 키로 매핑한다."""
    from app import settings
    from app.respond import policy

    class _FakeContainer:
        def query_items(self, query, parameters, partition_key):
            assert partition_key == "강원특별자치도"       # 시도 파티션으로 조회
            return [{"기관명": "○○시자살예방센터", "전화": "033-000-0000",
                     "주소": "강원특별자치도 ○○시 ○○로 00", "유형": "기초 자살예방센터"}]

    monkeypatch.setattr(settings, "HOTLINE_CONTAINER", "kfsp_centers")
    monkeypatch.setattr(policy, "_get_hotline_container", lambda: _FakeContainer())
    out = policy.lookup_regional_hotlines("강원특별자치도")
    assert out == [{"name": "○○시자살예방센터", "phone": "033-000-0000",
                    "address": "강원특별자치도 ○○시 ○○로 00", "type": "기초 자살예방센터"}]


def test_region_resolver_precedence(monkeypatch):
    """resolve_region: metadata.region override 가 프로필 DB 조회보다 우선한다.
    프로필 경로(DB 조회 루트) 배선은 가짜 프로필로 검증 — user_profiles 가 비어도 배선은 증명된다."""
    from app import settings
    from app.respond import policy
    monkeypatch.setattr(settings, "USER_PROFILE_CONTAINER", "user_profiles")
    monkeypatch.setattr(policy, "_region_from_profile", lambda uid: ("부산광역시", "해운대구"))

    # 1) metadata.region 이 있으면 프로필을 보지 않고 그대로 (시도, 시군구=None)
    assert policy.resolve_region({"metadata": {"region": "서울특별시"}}, user_id="u1") == ("서울특별시", None)
    # 2) metadata 에 지역이 없고 user_id 가 있으면 프로필 DB 조회 루트로 폴백
    assert policy.resolve_region({"metadata": {}}, user_id="u1") == ("부산광역시", "해운대구")
    # 3) 둘 다 없으면 (None, None) — 전국 공통만
    assert policy.resolve_region({}, user_id=None) == (None, None)


def test_respond_transcript_path(gateway):
    client, _ = gateway
    events = sse_events(client.post("/v1/respond", json={"stt": {"transcript": "전사된 문장입니다"}}))
    assert types_of(events) == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]


def test_respond_audio_stt_success(gateway):
    client, _ = gateway
    events = sse_events(client.post("/v1/respond", json={
        "audio": {"kind": "base64", "data": "QUJD", "mime_type": "audio/wav"}}))
    assert types_of(events)[:2] == ["stt", "stt"]
    assert events[0]["status"] == "processing"
    assert events[1]["status"] == "completed" and events[1]["transcript"]
    assert types_of(events)[2:] == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]


def test_respond_audio_stt_failure(gateway, monkeypatch):
    client, services = gateway
    monkeypatch.setattr(services, "speech", FakeSpeech(stt_ok=False))
    events = sse_events(client.post("/v1/respond", json={"audio": {"kind": "base64", "data": "QUJD"}}))
    assert types_of(events) == ["stt", "stt", "input_required", "done"]
    assert events[1]["status"] == "no_match"
    assert events[2]["reason"] == "no_match"


def test_respond_no_input(gateway):
    client, _ = gateway
    events = sse_events(client.post("/v1/respond", json={}))
    assert types_of(events) == ["meta", "input_required", "done"]
    assert events[1]["reason"] == "text_required"


def test_respond_tts_enabled(gateway):
    client, _ = gateway
    events = sse_events(client.post("/v1/respond", json={"text": "안녕하세요", "tts": {"enabled": True}}))
    assert types_of(events) == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["tts", "done"]
    tts = next(e for e in events if e["type"] == "tts")
    assert tts["status"] == "completed" and tts["audio"]["kind"] == "base64"


def test_api_key_required(gateway, monkeypatch):
    client, _ = gateway
    from app import settings
    monkeypatch.setattr(settings, "API_KEY_REQUIRED", True)
    monkeypatch.setattr(settings, "API_KEY", "secret-key")
    assert client.post("/v1/classify", json={"text": "x"}).status_code == 401
    ok = client.post("/v1/classify", json={"text": "x"}, headers={"x-api-key": "secret-key"})
    assert ok.status_code == 200 and ok.json()["primary"] == CLS_RESULT["primary"]


class _FakeVerifier:
    """Entra JWT 검증기 대역 — 네트워크·PyJWT 없이 토큰 게이트만 검증."""
    def verify(self, token):
        if token == "good-token":
            return "user-abc-123"
        raise ValueError("bad token")


def _enable_entra(monkeypatch):
    from app import settings
    from app.api import v1
    monkeypatch.setattr(settings, "AUTH_MODE", "entra")
    monkeypatch.setattr(v1, "_verifier", _FakeVerifier())  # 실제 검증기 생성 차단


def test_entra_valid_token_passes(gateway, monkeypatch):
    client, _ = gateway
    _enable_entra(monkeypatch)
    r = client.post("/v1/classify", json={"text": "x"},
                    headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200 and r.json()["primary"] == CLS_RESULT["primary"]


def test_entra_missing_token_401(gateway, monkeypatch):
    client, _ = gateway
    _enable_entra(monkeypatch)
    assert client.post("/v1/classify", json={"text": "x"}).status_code == 401


def test_entra_bad_token_401(gateway, monkeypatch):
    client, _ = gateway
    _enable_entra(monkeypatch)
    r = client.post("/v1/classify", json={"text": "x"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_entra_misconfigured_500(gateway, monkeypatch):
    """AUTH_MODE=entra 인데 ENTRA_* 설정이 없으면 500(서버 오설정)으로 명확히 실패."""
    client, _ = gateway
    from app import settings
    from app.api import v1
    monkeypatch.setattr(settings, "AUTH_MODE", "entra")
    monkeypatch.setattr(v1, "_verifier", None)           # 실제 생성 경로로
    monkeypatch.setattr(settings, "ENTRA_CLIENT_ID", "")
    monkeypatch.setattr(settings, "ENTRA_ISSUER", "")
    monkeypatch.setattr(settings, "ENTRA_TENANT_ID", "")
    r = client.post("/v1/classify", json={"text": "x"},
                    headers={"Authorization": "Bearer any"})
    assert r.status_code == 500


def test_batch_classify(gateway):
    client, _ = gateway
    r = client.post("/v1/batch-classify", json={"texts": ["a", "b"]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert [i["index"] for i in results] == [0, 1]
    assert all(i["ok"] and i["result"]["primary"] for i in results)


def test_session_turn_count_increases(gateway):
    client, _ = gateway
    created = client.post("/v1/sessions", json={}).json()
    sid = created["session_id"]
    assert created["turn_count"] == 0
    client.post("/v1/respond", json={"text": "첫 발화", "session_id": sid})
    snap = client.get(f"/v1/sessions/{sid}").json()
    assert snap["turn_count"] == 2  # user + assistant
    assert [t["role"] for t in snap["turns"]] == ["user", "assistant"]


def test_session_not_found(gateway):
    client, _ = gateway
    assert client.get("/v1/sessions/does-not-exist-123").status_code == 404
