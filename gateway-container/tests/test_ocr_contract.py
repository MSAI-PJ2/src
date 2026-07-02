"""이미지 입력 → OCR → 상담 흐름의 SSE 계약 테스트 (generic / kakao 프로파일).

외부 서비스(Document Intelligence 포함)는 전부 가짜 어댑터로 대체 — 키 없이 실행.
kakao 파이프라인 원본: di/kakao_ocr_pipeline.py (팀원 작업물), 복제본: app/services/document_ocr.py.
"""
import pytest
from fastapi.testclient import TestClient

from test_v1_contract import (  # 기존 가짜 어댑터·헬퍼 재사용
    LLM_TOKENS, FakeClassifier, FakeLlm, FakeRetriever, FakeSafety, FakeSpeech,
    sse_events, types_of,
)

CONVERSATION = [
    {"speaker": "감동받은 어피치", "content": "야 오늘 과제 제출했어?", "time": "오전 11:15"},
    {"speaker": "나", "content": "응 아까 냈어.", "time": "오전 11:15"},
    {"speaker": "나", "content": "근데 나는 뭘 해도 안 되는 것 같아", "time": "오전 11:16"},
]
KAKAO_USER_TEXT = "응 아까 냈어.\n근데 나는 뭘 해도 안 되는 것 같아"
GENERIC_TEXT = "오늘도 발표를 망칠 것 같다는 생각이 하루 종일 떠나지 않았다."


class FakeDocument:
    """profile 에 따라 kakao(대화 로그) / generic(전체 텍스트) 결과를 흉내낸다."""

    def __init__(self, status="completed", conversation=None, generic_text=GENERIC_TEXT):
        self.status = status
        self.conversation = CONVERSATION if conversation is None else conversation
        self.generic_text = generic_text
        self.calls = []

    async def extract(self, image, sender_names=None, profile="generic"):
        self.calls.append({"image": image, "sender_names": sender_names, "profile": profile})
        base = {"provider": "azure_document_intelligence", "profile": profile}
        if self.status != "completed":
            return {**base, "status": self.status, "conversation": [], "user_text": "", "error": "boom"}
        if profile == "kakao":
            user_text = "\n".join(m["content"] for m in self.conversation if m["speaker"] == "나")
            return {**base, "status": "completed", "conversation": self.conversation,
                    "user_text": user_text}
        return {**base, "status": "completed", "conversation": [], "user_text": self.generic_text}


@pytest.fixture()
def gateway(monkeypatch):
    from app.services import services
    monkeypatch.setattr(services, "classifier", FakeClassifier())
    monkeypatch.setattr(services, "safety", FakeSafety(safe=True))
    monkeypatch.setattr(services, "retriever", FakeRetriever())
    monkeypatch.setattr(services, "llm", FakeLlm())
    monkeypatch.setattr(services, "speech", FakeSpeech())
    fake_doc = FakeDocument()
    monkeypatch.setattr(services, "document", fake_doc)

    from app.main import app
    return TestClient(app), services, fake_doc


IMAGE = {"kind": "base64", "data": "QUJD", "mime_type": "image/png"}
KAKAO_BODY = {"image": IMAGE, "ocr": {"profile": "kakao", "sender_names": ["감동받은 어피치"]}}
GENERIC_BODY = {"image": IMAGE}  # ocr 생략 = generic 기본값


def test_kakao_profile_event_sequence(gateway):
    client, _, fake_doc = gateway
    r = client.post("/v1/respond", json=KAKAO_BODY)
    assert r.status_code == 200
    events = sse_events(r)

    # ocr(processing) → ocr(completed, conversation) → 기존 텍스트 상담 시퀀스
    assert types_of(events)[:2] == ["ocr", "ocr"]
    assert events[0]["status"] == "processing"
    assert events[1]["status"] == "completed"
    assert events[1]["profile"] == "kakao"
    assert events[1]["conversation"] == CONVERSATION
    assert events[1]["user_text"] == KAKAO_USER_TEXT
    assert types_of(events)[2:] == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]

    meta = events[2]
    assert meta["input"]["input_type"] == "image"
    assert "data" not in (meta["input"]["image"] or {})   # 원본 base64 는 저장/노출 금지
    assert meta["input"]["ocr"]["profile"] == "kakao"
    assert meta["input"]["ocr"]["conversation"] == CONVERSATION

    # profile·sender_names 가 어댑터까지 전달됐는지
    assert fake_doc.calls[0]["profile"] == "kakao"
    assert fake_doc.calls[0]["sender_names"] == ["감동받은 어피치"]


def test_generic_profile_event_sequence(gateway):
    """일반 이미지: 추출 텍스트 전체가 사용자 발화가 된다 (화자 분리 없음)."""
    client, _, fake_doc = gateway
    events = sse_events(client.post("/v1/respond", json=GENERIC_BODY))

    assert types_of(events)[:2] == ["ocr", "ocr"]
    assert events[1]["profile"] == "generic"
    assert events[1]["user_text"] == GENERIC_TEXT
    assert types_of(events)[2:] == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]
    meta = events[2]
    assert meta["input"]["ocr"]["profile"] == "generic"
    assert "conversation" not in meta["input"]["ocr"]     # generic 은 대화 로그 없음
    assert fake_doc.calls[0]["profile"] == "generic"


def test_ocr_failure(gateway, monkeypatch):
    client, services, _ = gateway
    monkeypatch.setattr(services, "document", FakeDocument(status="error"))
    events = sse_events(client.post("/v1/respond", json=KAKAO_BODY))
    assert types_of(events) == ["ocr", "ocr", "input_required", "done"]
    assert events[1]["status"] == "error"
    assert events[2]["reason"] == "error"


def test_kakao_no_user_messages(gateway, monkeypatch):
    """상대방 발화만 있는 캡쳐: 상담할 '나' 발화가 없으므로 재입력을 요청한다."""
    client, services, _ = gateway
    only_other = [{"speaker": "감동받은 어피치", "content": "야", "time": None}]
    monkeypatch.setattr(services, "document", FakeDocument(conversation=only_other))
    events = sse_events(client.post("/v1/respond", json=KAKAO_BODY))
    assert types_of(events) == ["ocr", "ocr", "input_required", "done"]
    assert events[1]["status"] == "no_user_messages"
    assert events[2]["reason"] == "no_user_messages"


def test_generic_no_text_found(gateway, monkeypatch):
    """텍스트가 없는 일반 이미지: no_text_found 로 재입력을 요청한다."""
    client, services, _ = gateway
    monkeypatch.setattr(services, "document", FakeDocument(generic_text=""))
    events = sse_events(client.post("/v1/respond", json=GENERIC_BODY))
    assert types_of(events) == ["ocr", "ocr", "input_required", "done"]
    assert events[1]["status"] == "no_text_found"


def test_text_still_wins_over_image(gateway):
    """text 가 함께 오면 OCR 을 건너뛰고 일반 텍스트 상담으로 처리한다."""
    client, _, fake_doc = gateway
    events = sse_events(client.post("/v1/respond", json={**KAKAO_BODY, "text": "직접 쓴 발화"}))
    assert types_of(events) == ["meta", "chunks"] + ["token"] * len(LLM_TOKENS) + ["done"]
    assert fake_doc.calls == []


def test_parsing_logic_matches_original():
    """복제한 순수 파싱 함수 검증 — 좌우 화자·이름 매칭·y좌표 타임스탬프 (원본 di/ 알고리즘)
    + generic/kakao 프로파일의 user_text 추출."""
    from app.services.document_ocr import PROFILES

    page = {"width": 1000, "height": 2000, "lines": [
        {"content": "감동받은 어피치", "polygon": [100, 100, 300, 100, 300, 140, 100, 140]},
        {"content": "야 오늘 과제 제출했어?", "polygon": [100, 160, 400, 160, 400, 200, 100, 200]},
        {"content": "오전 11:15", "polygon": [420, 300, 500, 300, 500, 330, 420, 330]},
        {"content": "응 아까 냈어.", "polygon": [600, 290, 900, 290, 900, 330, 600, 330]},
    ]}

    kakao = PROFILES["kakao"](page, ["감동받은 어피치"])
    assert kakao["conversation"] == [
        {"speaker": "감동받은 어피치", "content": "야 오늘 과제 제출했어?", "time": None},
        {"speaker": "나", "content": "응 아까 냈어.", "time": "오전 11:15"},
    ]
    assert kakao["user_text"] == "응 아까 냈어."   # "나" 발화만

    generic = PROFILES["generic"](page, None)
    assert generic["conversation"] == []
    # generic 은 화자 구분 없이 모든 라인을 그대로 이어붙인다
    assert generic["user_text"].splitlines() == [
        "감동받은 어피치", "야 오늘 과제 제출했어?", "오전 11:15", "응 아까 냈어."]
