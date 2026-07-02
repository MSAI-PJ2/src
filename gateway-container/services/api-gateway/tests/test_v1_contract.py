"""/v1 API 계약(characterization) 테스트.

목적: 리팩토링 전후로 프론트엔드가 의존하는 v1 SSE 이벤트 계약이 깨지지 않았음을
증명한다. 외부 서비스(cogdist·Content Safety·AI Search·Azure OpenAI·Speech)는
전부 가짜 어댑터로 대체하므로 네트워크·키 없이 로컬에서 실행된다.

검증하는 계약 (API_CONTRACT.md 기준):
    text/transcript 응답:  meta → chunks → token* → done
    crisis 분기:           meta → crisis → done
    audio STT 성공:        stt(processing) → stt(completed) → meta → ... → done
    audio STT 실패:        stt(processing) → stt(실패) → input_required → done
    입력 없음:             meta → input_required → done
    TTS 활성:              ... → token* → tts → done
    인증:                  API_KEY_REQUIRED=true 이면 x-api-key 없을 때 401
    세션:                  respond 후 turn_count 증가
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 레이아웃 독립 import — 리팩토링 전(app.adapters/app.settings)과
# 후(app.services/app.core.settings) 어느 쪽에서도 같은 테스트가 동작한다.
# ---------------------------------------------------------------------------

def _import_services():
    try:
        from app.services import services  # 리팩토링 후 배치
        return services
    except ImportError:
        from app.adapters import services  # 리팩토링 전 배치
        return services


def _import_settings():
    try:
        from app.core import settings  # 리팩토링 후 배치
        return settings
    except ImportError:
        from app import settings  # 리팩토링 전 배치
        return settings


# ---------------------------------------------------------------------------
# 가짜 어댑터 — 외부 서비스 호출을 결정적(deterministic) 응답으로 대체
# ---------------------------------------------------------------------------

CLS_RESULT = {
    "text": "테스트 발화",
    "mode": "single",
    "model": "cogdist-test",
    "model_version": "test",
    "threshold": 0.5,
    "primary": "흑백 사고",
    "labels": [{"label": "흑백 사고", "score": 0.91, "selected": True}],
}

CANDIDATES = [
    {"id": "d1", "content": "근거 검토 기법", "score": 0.9,
     "metadata": {"distortions": ["흑백 사고"]}},
    {"id": "d2", "content": "탈파국화 기법", "score": 0.5, "metadata": {}},
]

LLM_TOKENS = ["괜찮아요, ", "함께 살펴봐요."]


class FakeClassifier:
    async def classify_one(self, text, threshold=None):
        return {**CLS_RESULT, "text": text}

    async def classify_batch(self, texts, threshold=None):
        return {"results": [
            {"index": i, "ok": True, "result": {**CLS_RESULT, "text": t}, "error": None}
            for i, t in enumerate(texts)
        ]}


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
    """리팩토링 전(sync generator)과 후(async generator) 인터페이스를 모두 제공."""

    def chat_stream(self, messages, options=None):
        yield from LLM_TOKENS

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
    """가짜 어댑터가 주입된 TestClient. 필요 시 개별 테스트에서 어댑터 교체."""
    services = _import_services()
    monkeypatch.setattr(services, "classifier", FakeClassifier())
    monkeypatch.setattr(services, "safety", FakeSafety(safe=True))
    monkeypatch.setattr(services, "retriever", FakeRetriever())
    monkeypatch.setattr(services, "llm", FakeLlm())
    monkeypatch.setattr(services, "speech", FakeSpeech(stt_ok=True))

    from app.main import app
    return TestClient(app), services


def sse_events(response) -> list[dict]:
    """SSE 본문을 이벤트 dict 리스트로 파싱."""
    events = []
    for frame in response.text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data: "):
            events.append(json.loads(frame[len("data: "):]))
    return events


def types_of(events) -> list[str]:
    return [e["type"] for e in events]


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

def test_healthz(gateway):
    client, _ = gateway
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


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

    chunks = events[1]["chunks"]
    assert all(set(c) == {"id", "content"} for c in chunks)

    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert tokens == "".join(LLM_TOKENS)

    assert all(e["session_id"] == meta["session_id"] for e in events)


def test_respond_crisis_branch(gateway, monkeypatch):
    client, services = gateway
    monkeypatch.setattr(services, "safety", FakeSafety(safe=False))

    r = client.post("/v1/respond", json={"text": "더 살 이유가 없는 것 같아요"})
    events = sse_events(r)

    assert types_of(events) == ["meta", "crisis", "done"]
    crisis = events[1]
    assert crisis["blocked"] is True
    assert crisis["message"]
    assert isinstance(crisis["resources"], list) and crisis["resources"]
    assert all({"name", "phone"} <= set(res) for res in crisis["resources"])


def test_respond_transcript_path(gateway):
    client, _ = gateway
    r = client.post("/v1/respond", json={"stt": {"transcript": "전사된 문장입니다"}})
    events = sse_events(r)
    assert types_of(events) == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]


def test_respond_audio_stt_success(gateway):
    client, _ = gateway
    r = client.post("/v1/respond", json={"audio": {"kind": "base64", "data": "QUJD", "mime_type": "audio/wav"}})
    events = sse_events(r)

    assert types_of(events)[:2] == ["stt", "stt"]
    assert events[0]["status"] == "processing"
    assert events[1]["status"] == "completed"
    assert events[1]["transcript"]
    assert types_of(events)[2:] == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]


def test_respond_audio_stt_failure(gateway, monkeypatch):
    client, services = gateway
    monkeypatch.setattr(services, "speech", FakeSpeech(stt_ok=False))

    r = client.post("/v1/respond", json={"audio": {"kind": "base64", "data": "QUJD"}})
    events = sse_events(r)

    assert types_of(events) == ["stt", "stt", "input_required", "done"]
    assert events[1]["status"] == "no_match"
    assert events[2]["reason"] == "no_match"


def test_respond_no_input(gateway):
    client, _ = gateway
    r = client.post("/v1/respond", json={})
    events = sse_events(r)
    assert types_of(events) == ["meta", "input_required", "done"]
    assert events[1]["reason"] == "text_required"


def test_respond_tts_enabled(gateway):
    client, _ = gateway
    r = client.post("/v1/respond", json={"text": "안녕하세요", "tts": {"enabled": True}})
    events = sse_events(r)

    assert types_of(events) == (
        ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["tts", "done"]
    )
    tts = [e for e in events if e["type"] == "tts"][0]
    assert tts["status"] == "completed"
    assert tts["audio"]["kind"] == "base64"


def test_api_key_required(gateway, monkeypatch):
    client, _ = gateway
    settings = _import_settings()
    monkeypatch.setattr(settings, "API_KEY_REQUIRED", True)
    monkeypatch.setattr(settings, "API_KEY", "secret-key")

    assert client.post("/v1/classify", json={"text": "x"}).status_code == 401
    ok = client.post("/v1/classify", json={"text": "x"}, headers={"x-api-key": "secret-key"})
    assert ok.status_code == 200
    assert ok.json()["primary"] == CLS_RESULT["primary"]


def test_batch_classify(gateway):
    client, _ = gateway
    r = client.post("/v1/batch-classify", json={"texts": ["a", "b"]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert [item["index"] for item in results] == [0, 1]
    assert all(item["ok"] and item["result"]["primary"] for item in results)


def test_session_turn_count_increases(gateway):
    client, _ = gateway
    created = client.post("/v1/sessions", json={}).json()
    sid = created["session_id"]
    assert created["turn_count"] == 0

    client.post("/v1/respond", json={"text": "첫 발화", "session_id": sid})

    snap = client.get(f"/v1/sessions/{sid}").json()
    assert snap["turn_count"] == 2  # user + assistant
    roles = [t["role"] for t in snap["turns"]]
    assert roles == ["user", "assistant"]


def test_session_not_found(gateway):
    client, _ = gateway
    assert client.get("/v1/sessions/does-not-exist-123").status_code == 404
