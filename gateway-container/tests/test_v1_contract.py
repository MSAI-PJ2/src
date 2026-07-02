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


def test_auth_mode_entra_fails_fast(gateway, monkeypatch):
    """AUTH_MODE=entra 는 구현 전까지 501 로 명시적으로 실패해야 한다 (api/v1.py 구획 2)."""
    client, _ = gateway
    from app import settings
    monkeypatch.setattr(settings, "AUTH_MODE", "entra")
    assert client.post("/v1/classify", json={"text": "x"}).status_code == 501


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
