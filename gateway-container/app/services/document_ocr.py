"""[문서 창구] 채팅 캡쳐 이미지 → 대화 로그 (OCR) — Azure Document Intelligence.

┌─ 출처 ────────────────────────────────────────────────────────────────┐
│ 원본: 리포지토리 루트 di/kakao_ocr_pipeline.py (DI 담당 팀원 작업물).   │
│ 원본은 포트폴리오/독립 실행 목적으로 무수정 보존하고, 게이트웨이 통합을 │
│ 위해 이 파일로 복제·개조했다. 알고리즘 설명은 di/README.md 가 기준 문서.│
│                                                                       │
│ 핵심 알고리즘(원본과 동일):                                            │
│   1. DI prebuilt-read 로 텍스트 라인 + 좌표(polygon) 추출               │
│   2. 라인 분류: 타임스탬프(오전/오후 HH:MM) / 발신자 이름 / 메시지      │
│   3. 화자 판별: 말풍선 x좌표가 화면 중앙 기준 오른쪽=나, 왼쪽=상대방     │
│   4. 타임스탬프를 y좌표 근접도로 메시지에 매칭                          │
│                                                                       │
│ 개조 내용(게이트웨이 통합용):                                          │
│   - 파일 경로 대신 bytes 입력 (업로드된 이미지를 바로 처리)             │
│   - print/JSON 파일 저장 제거, dict 반환 (SSE 이벤트·세션 저장용)       │
│   - dotenv 제거 (환경변수 직접 읽음), 클라이언트 재사용                 │
└───────────────────────────────────────────────────────────────────────┘

필요 환경변수: DOCINTEL_ENDPOINT, DOCINTEL_KEY
DI SDK 는 블로킹이라 어댑터가 asyncio.to_thread 로 오프로딩한다.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re

import httpx

_client = None  # DocumentIntelligenceClient — 첫 호출 시 생성해 재사용


def _di_client():
    global _client
    if _client is None:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        endpoint = os.environ.get("DOCINTEL_ENDPOINT", "")
        key = os.environ.get("DOCINTEL_KEY", "")
        if not endpoint or not key:
            raise ValueError("Document Intelligence requires DOCINTEL_ENDPOINT + DOCINTEL_KEY")
        _client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    return _client


def resolve_image_bytes(image: dict) -> bytes:
    """요청의 image 필드에서 실제 이미지 바이트를 꺼낸다 (speech.py 의 오디오 패턴과 동일)."""
    kind = image.get("kind")
    if kind == "base64":
        data = image.get("data")
        if not data:
            raise ValueError("image.data is required when image.kind='base64'")
        # 브라우저가 "data:image/png;base64,...." 형태로 보내는 경우 앞부분을 떼어낸다
        if isinstance(data, str) and data.strip().startswith("data:") and "," in data:
            data = data.split(",", 1)[1]
        return base64.b64decode(data)
    if kind == "url":
        url = image.get("url")
        if not url:
            raise ValueError("image.url is required when image.kind='url'")
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        return resp.content
    raise ValueError(f"unsupported image.kind: {kind!r} (base64 | url)")


def analyze_image_bytes(image_bytes: bytes) -> dict:
    """DI prebuilt-read 호출 → 첫 페이지의 텍스트 라인 + 좌표. (원본 analyze_image 의 bytes 판)"""
    poller = _di_client().begin_analyze_document("prebuilt-read", body=image_bytes)
    result = poller.result()
    page = result.pages[0]  # 원본과 동일하게 첫 페이지만 처리 (다중 캡쳐는 개선 후보)
    return {
        "width": page.width,
        "height": page.height,
        "lines": [{"content": line.content, "polygon": line.polygon} for line in page.lines],
    }


# ---------------------------------------------------------------------------
# 이하 순수 파싱 함수들 — di/kakao_ocr_pipeline.py 의 로직을 그대로 복제
# (Azure 무관이라 단위테스트 가능: tests/test_ocr_contract.py 참고)
# ---------------------------------------------------------------------------

def is_time_stamp(text: str) -> bool:
    return bool(re.fullmatch(r"(오전|오후)\s*\d{1,2}:\d{2}", text.strip()))


def classify_speaker(polygon, page_width: float) -> str:
    """말풍선 왼쪽 x좌표가 화면 중앙보다 왼쪽이면 상대방, 오른쪽이면 나."""
    x_left = polygon[0]
    midpoint = page_width / 2
    return "상대방" if x_left < midpoint else "나"


def parse_lines(page_data: dict, known_sender_names: set) -> list:
    """OCR 라인을 message / timestamp / sender_name 으로 분류한다.

    known_sender_names: 채팅방 상단에 뜨는 상대 이름 목록(요청의 ocr.sender_names).
    지정하면 이름 라벨과 실제 메시지를 더 정확히 구분한다.
    """
    page_width = page_data["width"]
    parsed = []
    for line in page_data["lines"]:
        content = line["content"]
        polygon = line["polygon"]
        if is_time_stamp(content):
            parsed.append({"type": "timestamp", "speaker": None, "content": content, "polygon": polygon})
        elif content.strip() in known_sender_names:
            parsed.append({"type": "sender_name", "speaker": "상대방", "content": content, "polygon": polygon})
        else:
            speaker = classify_speaker(polygon, page_width)
            parsed.append({"type": "message", "speaker": speaker, "content": content, "polygon": polygon})
    return parsed


def polygon_center_y(polygon):
    ys = polygon[1::2]
    return sum(ys) / len(ys)


def build_conversation(parsed_lines: list) -> list:
    """분류된 라인들을 화자·시간이 매칭된 대화 로그로 재구성한다."""
    messages = []
    current_name = None
    for item in parsed_lines:
        if item["type"] == "sender_name":
            current_name = item["content"]
        elif item["type"] == "message":
            speaker_name = "나" if item["speaker"] == "나" else (current_name or "상대방")
            messages.append({"speaker": speaker_name, "content": item["content"],
                             "time": None, "_y": polygon_center_y(item["polygon"])})

    # 타임스탬프는 읽기 순서가 아니라 y좌표가 가장 가까운 메시지에 매칭 (원본 로직)
    timestamps = [{"content": item["content"], "_y": polygon_center_y(item["polygon"])}
                  for item in parsed_lines if item["type"] == "timestamp"]
    for ts in timestamps:
        candidates = [m for m in messages if m["time"] is None]
        if not candidates:
            break
        closest = min(candidates, key=lambda m: abs(m["_y"] - ts["_y"]))
        closest["time"] = ts["content"]

    for m in messages:
        m.pop("_y", None)
    return messages


# ---------------------------------------------------------------------------
# 프로파일 — 이미지 종류별 해석 방법. 새 메신저/문서 형식은 여기에 함수를 추가하고
# PROFILES 에 한 줄 등록하면 된다 (요청의 ocr.profile 값과 짝).
# ---------------------------------------------------------------------------

def _parse_kakao(page_data: dict, sender_names: list[str] | None) -> dict:
    """카카오톡 캡쳐: 팀원 파이프라인으로 화자를 분리하고 "나"(내담자) 발화만 상담 입력으로."""
    conversation = build_conversation(parse_lines(page_data, set(sender_names or [])))
    user_text = "\n".join(m.get("content", "") for m in conversation
                          if m.get("speaker") == "나").strip()
    return {"conversation": conversation, "user_text": user_text}


def _parse_generic(page_data: dict, sender_names: list[str] | None) -> dict:
    """일반 이미지(일기·메모 등): 레이아웃 가정 없이 추출된 텍스트 전체를 사용자 발화로."""
    text = "\n".join(line["content"] for line in page_data["lines"]).strip()
    return {"conversation": [], "user_text": text}


PROFILES = {
    "kakao": _parse_kakao,      # 카톡 캡쳐 — 좌우 화자 판별 (원본: di/ 파이프라인)
    "generic": _parse_generic,  # 일반 이미지 — 텍스트 전체 (기본값)
}


def extract(image: dict, sender_names: list[str] | None = None,
            profile: str = "generic") -> dict:
    """이미지 dict → OCR 해석 결과. SSE `ocr` 이벤트 계약(dict)으로 반환하며 예외도 error 로 감싼다.

    반환: {provider, profile, status: completed|error,
           user_text(상담 입력으로 쓸 텍스트), conversation(kakao 프로파일만 채워짐), error?}
    """
    base = {"provider": "azure_document_intelligence", "profile": profile,
            "kind": image.get("kind") if image else None}
    parser = PROFILES.get(profile)
    if parser is None:
        return {**base, "status": "error", "conversation": [], "user_text": "",
                "error": f"unsupported ocr.profile: {profile!r} (지원: {', '.join(PROFILES)})"}
    try:
        raw = resolve_image_bytes(image or {})
        page_data = analyze_image_bytes(raw)
        return {**base, "status": "completed", **parser(page_data, sender_names)}
    except Exception as exc:
        return {**base, "status": "error", "conversation": [], "user_text": "",
                "error": str(exc)[:300]}


class DocumentAdapter:
    async def extract(self, image: dict | None, sender_names: list[str] | None = None,
                      profile: str = "generic") -> dict:
        """이미지 → {status, profile, user_text, conversation, error?} (ocr 이벤트 형식)."""
        return await asyncio.to_thread(extract, image, sender_names, profile)
