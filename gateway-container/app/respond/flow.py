"""[상담 흐름 — 기계장치] 상담 한 턴이 "어떻게" 흘러가는지가 전부 이 파일에 있다.

"무엇을 답할지"(정책·프롬프트·위기 문구)는 respond/policy.py — 이 파일은 순서와 배관만.

구획 목차 (Ctrl+F 로 "[구획" 검색):
    [구획 1] SSE 이벤트     프론트로 내보내는 메시지 조각들의 형식 (API_CONTRACT 와 1:1)
    [구획 2] 요청 정리      "텍스트/음성/이미지 중 뭐가 왔나" 판단 (RespondRequestContext)
    [구획 3] RAG 재정렬     검색 결과를 프롬프트에 넣을 순서로 다듬기 (+ 튜닝 노브)
    [구획 4] 진입 스트림    stt(음성)/ocr(이미지)/입력없음 — 성공하면 [구획 5]로 합류
    [구획 5] respond_stream 핵심 6단계: 세션→병렬분석→정책→(위기)→LLM 스트리밍→저장

읽는 법 — 스트림 함수들은 "제너레이터"다:
    yield sse(...) = "이벤트 하나를 프론트로 지금 내보내라". return 처럼 끝나지 않고
    다음 줄로 계속 진행하므로, 위에서 아래로 읽으면 프론트가 받는 이벤트 순서와 같다.
    await = Azure 응답을 기다리는 동안 다른 요청 처리를 양보한다는 표시.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .. import settings
from ..services import services
from ..session import (
    assistant_turn, crisis_turn, input_pending_turn, ocr_failed_turn,
    session_repository, stt_failed_turn, user_turn,
)
from . import policy as respond_policy

DEFAULT_LANGUAGE = "ko-KR"


# ══════════════════════════════════════════════════════════════════════════
# [구획 1] SSE 이벤트 — 스트리밍으로 프론트엔드에 보내는 메시지 조각들의 형식
#
# SSE(Server-Sent Events) = 서버가 응답을 끊지 않고 "data: {...}" 줄을 계속
# 흘려보내는 방식. 프론트는 type 필드로 구분해 화면에 반영한다.
# 이벤트 종류/필드는 API_CONTRACT.md 와 1:1 — 여기를 바꾸면 프론트도 바꿔야 한다.
# DB 에 저장하는 대화 기록은 session.py 의 턴 빌더 — 역할이 다르므로 섞지 않는다.
# ══════════════════════════════════════════════════════════════════════════

INPUT_REQUIRED_STT_MESSAGE = (
    "audio payload was accepted, but STT did not produce a transcript. "
    "Check stt event error/reason, or send text/stt.transcript."
)
INPUT_REQUIRED_TEXT_MESSAGE = (
    "No text or transcript was provided. Send text, stt.transcript, or an audio payload."
)
INPUT_REQUIRED_OCR_MESSAGE = (
    "image payload was accepted, but OCR did not produce user messages. "
    "Check ocr event error/reason, or send text instead."
)


def sse(obj: dict) -> str:
    """dict 하나를 SSE 한 프레임("data: {...}\\n\\n")으로 직렬화한다."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def stt_processing_event(session_id: str, provider: str, language: str) -> dict:
    """"음성 인식을 시작했다"는 알림 — 프론트가 로딩 표시를 띄울 수 있게."""
    return {"type": "stt", "session_id": session_id, "status": "processing",
            "provider": provider, "language": language}


def stt_result_event(session_id: str, result: dict) -> dict:
    """음성 인식 결과 (성공: transcript 포함 / 실패: error·reason 포함)."""
    return {"type": "stt", "session_id": session_id, **result}


def ocr_processing_event(session_id: str) -> dict:
    """"이미지 인식을 시작했다"는 알림."""
    return {"type": "ocr", "session_id": session_id, "status": "processing",
            "provider": "azure_document_intelligence"}


def ocr_result_event(session_id: str, result: dict) -> dict:
    """OCR 결과 (성공: conversation 포함 / 실패: error 포함)."""
    return {"type": "ocr", "session_id": session_id, **result}


def input_required_event(session_id: str, reason: str, message: str) -> dict:
    """처리할 입력이 없거나 STT/OCR 실패 — 사용자에게 재입력을 요청한다."""
    return {"type": "input_required", "session_id": session_id, "reason": reason, "message": message}


def meta_event(session_id: str, turn_count: int, input_meta: dict, tts: dict | None,
               cls: dict | None = None) -> dict:
    """턴 시작 정보: 몇 번째 턴인지 + 인지왜곡 분류 결과(primary/labels)."""
    payload = {"type": "meta", "session_id": session_id, "turn_count": turn_count,
               "input": input_meta, "tts": tts}
    if cls:
        payload.update({"primary": cls["primary"], "mode": cls["mode"], "labels": cls["labels"]})
    return payload


def chunks_event(session_id: str, chunks: list[dict]) -> dict:
    """RAG 로 검색된 참고자료 목록 (id 와 본문만 추려서 전달)."""
    return {"type": "chunks", "session_id": session_id,
            "chunks": [{"id": c["id"], "content": c["content"]} for c in chunks]}


def token_event(session_id: str, text: str) -> dict:
    """LLM 이 생성한 답변 조각 — 이 이벤트들을 이어붙이면 전체 답변이 된다."""
    return {"type": "token", "session_id": session_id, "text": text}


def tts_event(session_id: str, tts_result: dict) -> dict:
    """합성된 음성 (base64 오디오 포함) 또는 합성 실패 정보."""
    return {"type": "tts", "session_id": session_id, **tts_result}


def done_event(session_id: str) -> dict:
    """이 턴의 스트리밍이 끝났다는 신호 — 항상 마지막 이벤트."""
    return {"type": "done", "session_id": session_id}


# ══════════════════════════════════════════════════════════════════════════
# [구획 2] 요청 정리 — "이 요청이 텍스트인가, 음성인가, 이미지인가" 판단
#
# api/v1.py 가 요청을 받자마자 from_body() 로 이 객체를 만들고,
# requires_ocr / requires_stt / has_text 로 어느 흐름으로 보낼지 결정한다.
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)  # 읽기 전용 데이터 묶음 — 흐름 중간에 값이 바뀌는 실수를 막는다
class RespondRequestContext:
    session_id: str | None
    text: str | None               # 실제 처리할 텍스트 (text 또는 stt.transcript 에서 온 것)
    input_meta: dict[str, Any]     # 입력 형태 기록 (세션 저장·meta 이벤트용)
    tts: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None

    @classmethod
    def from_body(cls, body) -> "RespondRequestContext":
        """프론트 요청(api/v1.py 의 RespondIn) → 내부 컨텍스트로 변환."""
        return cls(
            session_id=body.session_id,
            text=body.effective_text(),
            input_meta=body.input_meta(),
            tts=body.tts.model_dump(exclude_none=True) if body.tts else None,
            llm=body.llm.model_dump(exclude_none=True) if body.llm else None,
        )

    # @property = 함수를 변수처럼 읽게 해 주는 문법 (context.has_text 처럼 괄호 없이 사용)

    @property
    def has_text(self) -> bool:
        """처리할 텍스트가 있는가?"""
        return bool((self.text or "").strip())

    @property
    def requires_stt(self) -> bool:
        """오디오만 있고 텍스트가 없어서 음성 인식(STT)이 먼저 필요한가?"""
        return bool(self.input_meta.get("audio")) and not self.has_text

    @property
    def requires_ocr(self) -> bool:
        """채팅 캡쳐 이미지만 있고 텍스트가 없어서 OCR 이 먼저 필요한가?"""
        return bool(self.input_meta.get("image")) and not self.has_text

    @property
    def audio(self) -> dict[str, Any]:
        return dict(self.input_meta.get("audio") or {})

    @property
    def image(self) -> dict[str, Any]:
        return dict(self.input_meta.get("image") or {})

    @property
    def sender_names(self) -> list[str]:
        """OCR 화자 판별 보정용 상대 이름 목록 (요청의 ocr.sender_names)."""
        return list((self.input_meta.get("ocr") or {}).get("sender_names") or [])

    @property
    def ocr_profile(self) -> str:
        """이미지 해석 방법: generic(일반 이미지, 기본) | kakao(카톡 캡쳐 — 화자 분리)."""
        return (self.input_meta.get("ocr") or {}).get("profile") or "generic"

    @property
    def language(self) -> str:
        """인식 언어: stt.language > audio.language > 기본값(ko-KR) 순서로 고른다."""
        stt = self.input_meta.get("stt") or {}
        return stt.get("language") or self.audio.get("language") or DEFAULT_LANGUAGE

    @property
    def stt_provider(self) -> str:
        return (self.input_meta.get("stt") or {}).get("provider") or "azure"

    def with_transcript(self, result: dict[str, Any]) -> "RespondRequestContext":
        """STT 성공 후: 전사문을 text 로 넣고 input_type 을 transcript 로 바꾼 새 컨텍스트."""
        input_meta = {
            **self.input_meta,
            "input_type": "transcript",
            "stt": {
                **(self.input_meta.get("stt") or {}),
                "provider": result.get("provider"),
                "language": result.get("language") or self.language,
                "transcript": result.get("transcript"),
                "confidence": result.get("confidence"),
                "recognition_status": result.get("recognition_status"),
            },
        }
        return RespondRequestContext(self.session_id, result.get("transcript"),
                                     input_meta, self.tts, self.llm)


def default_text_input_meta(input_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """input_meta 없이 호출된 경우(내부 호출 등)의 기본 형태."""
    return input_meta or {"input_type": "text"}


# ══════════════════════════════════════════════════════════════════════════
# [구획 3] RAG 재정렬 — 검색 결과를 "프롬프트에 넣을 순서"로 다듬기
#
# 하는 일 3가지:
#   1. 점수 정규화 — 검색 점수를 0~1 범위로 (검색엔진마다 점수 크기가 달라서)
#   2. 라벨 가산점 — 이번 발화의 인지왜곡 라벨과 관련된 기법 문서에 +RERANK_BIAS_WEIGHT
#   3. 중복 제거 후 상위 top_n 개만 반환
# 가산점 발동 조건은 환경변수 노브로 조정 (settings.py 의 RERANK_BIAS_* 참고):
#   score(확신 점수, 기본) | selected | either
#   ※ cogdist v2 부터 primary 는 항상 selected=true — selected 소스는 왜곡 발화에
#     무조건 발동하므로 신뢰도 게이트가 필요하면 score 소스(+0.55)를 쓴다
# ══════════════════════════════════════════════════════════════════════════

def _bias_eligible(primary: str, confidence: float, cls_labels: list[dict] | None) -> bool:
    """이번 턴에 라벨 가산점을 줄 수 있는 상태인지 판정한다."""
    if primary in ("정상", "불충분"):
        return False
    by_score = confidence >= settings.RERANK_BIAS_MIN_CONFIDENCE
    by_selected = any(l.get("label") == primary and l.get("selected")
                      for l in (cls_labels or []))
    source = settings.RERANK_BIAS_SOURCE
    if source == "selected":
        return by_selected
    if source == "either":
        return by_score or by_selected
    return by_score  # 기본: score (현행 동작)


def rerank(candidates: list[dict], primary: str, confidence: float,
           top_n: int | None = None, cls_labels: list[dict] | None = None) -> list[dict]:
    top_n = top_n or settings.RERANK_TOP_N
    if not candidates:
        return []

    # 1) 정규화 준비: 최고점과 최저점 사이의 폭(span)을 구한다
    scores = [float(c.get("score", 0.0)) for c in candidates]
    min_score, max_score = min(scores), max(scores)
    span = max_score - min_score

    # 2) 가산점 발동 여부 (조건은 위 _bias_eligible — 환경변수로 조정)
    use_bias = _bias_eligible(primary, confidence, cls_labels)
    deduped: dict[str, dict] = {}

    for candidate in candidates:
        raw = float(candidate.get("score", 0.0))
        normalized = 1.0 if span == 0 else (raw - min_score) / span
        # 문서의 metadata.distortions = 이 문서(상담 기법)가 다루는 왜곡 라벨 목록
        distortions = candidate.get("metadata", {}).get("distortions", [])
        final = normalized + (settings.RERANK_BIAS_WEIGHT
                              if use_bias and primary in distortions else 0.0)
        ranked = {**candidate, "score": final}
        cid = ranked.get("id")
        # 같은 id 문서가 여러 번 오면 점수가 높은 쪽만 남긴다
        if cid not in deduped or final > deduped[cid]["score"]:
            deduped[cid] = ranked

    # 3) 점수 내림차순 정렬 후 상위 top_n 개
    return sorted(deduped.values(), key=lambda c: c["score"], reverse=True)[:top_n]


# ══════════════════════════════════════════════════════════════════════════
# [구획 4] 입력 형태별 진입 스트림 — 성공하면 전부 [구획 5] respond_stream 으로 합류
# ══════════════════════════════════════════════════════════════════════════

async def stt_then_respond_stream(session_id=None, input_meta=None, tts=None, llm=None):
    """오디오 입력 흐름: 음성→텍스트(STT) 변환 후, 성공하면 일반 상담 흐름으로 넘어간다."""
    context = RespondRequestContext(session_id, None, input_meta or {}, tts, llm)
    session = await session_repository.ensure(context.session_id)  # 세션이 없으면 새로 만든다
    session_id = session["session_id"]
    context = RespondRequestContext(session_id, None, context.input_meta, tts, llm)

    # "인식 시작" 알림을 먼저 보내고, Azure Speech 로 음성을 텍스트로 변환
    yield sse(stt_processing_event(session_id, context.stt_provider, context.language))
    result = await services.speech.transcribe_audio(context.audio)

    if result.get("status") != "completed" or not result.get("transcript"):
        # STT 실패: 조용히 넘어가지 않고 실패 이벤트 + "다시 입력해달라" 요청을 명시적으로 보낸다
        await session_repository.append_turn(session_id, stt_failed_turn(context.input_meta, result, tts))
        yield sse(stt_result_event(session_id, result))
        yield sse(input_required_event(session_id, result.get("status") or "stt_failed",
                                       INPUT_REQUIRED_STT_MESSAGE))
        yield sse(done_event(session_id))
        return

    # STT 성공: 전사문을 반영한 컨텍스트로 바꾸고 일반 상담 흐름을 이어서 실행
    context = context.with_transcript(result)
    yield sse(stt_result_event(session_id, result))
    async for event in respond_stream(context.text or "", session_id, context.input_meta, tts, llm):
        yield event  # respond_stream 이 내보내는 이벤트를 그대로 통과시킨다


async def ocr_then_respond_stream(session_id=None, input_meta=None, tts=None, llm=None):
    """이미지 입력: OCR → ocr 이벤트 → 추출된 사용자 텍스트로 일반 상담 흐름으로.

    STT 흐름과 대칭 구조. 이미지 해석은 ocr.profile 로 갈린다 (services/document_ocr.py):
        generic  일반 이미지(일기·메모) — 추출 텍스트 전체를 사용자 발화로 (기본)
        kakao    카톡 캡쳐 — 팀원 파이프라인(원본: di/)으로 화자 분리, "나" 발화만
    """
    context = RespondRequestContext(session_id, None, input_meta or {}, tts, llm)
    session = await session_repository.ensure(context.session_id)
    session_id = session["session_id"]

    # "인식 시작" 알림을 먼저 보내고, Document Intelligence 로 텍스트/대화를 추출
    yield sse(ocr_processing_event(session_id))
    result = await services.document.extract(context.image, context.sender_names,
                                             profile=context.ocr_profile)
    conversation = result.get("conversation") or []
    user_text = (result.get("user_text") or "").strip()

    # 세션에는 원본 base64 를 빼고 저장한다 (Cosmos 문서 크기 한도·비용 보호)
    slim_image = {k: v for k, v in context.image.items() if k != "data"}
    stored_meta = {**context.input_meta, "image": slim_image}

    if result.get("status") != "completed" or not user_text:
        # OCR 실패 또는 쓸 텍스트 없음: 실패 이벤트 + 재입력 요청을 명시적으로 보낸다
        if result.get("status") == "completed":
            # kakao: "나" 발화가 없음 / generic: 이미지에서 텍스트를 못 찾음
            reason = "no_user_messages" if context.ocr_profile == "kakao" else "no_text_found"
            result = {**result, "status": reason}
        await session_repository.append_turn(session_id, ocr_failed_turn(stored_meta, result, tts))
        yield sse(ocr_result_event(session_id, result))
        yield sse(input_required_event(session_id, result.get("status") or "ocr_failed",
                                       INPUT_REQUIRED_OCR_MESSAGE))
        yield sse(done_event(session_id))
        return

    # OCR 성공: 해석 결과를 입력 기록에 남기고 이벤트로 프론트에 전달
    ocr_meta = {**(stored_meta.get("ocr") or {}), "profile": context.ocr_profile}
    if conversation:
        ocr_meta["conversation"] = conversation  # kakao 프로파일: 대화 로그 보존
    stored_meta["ocr"] = ocr_meta
    yield sse(ocr_result_event(session_id, result))
    async for event in respond_stream(user_text, session_id, stored_meta, tts, llm):
        yield event


async def input_pending_stream(session_id=None, input_meta=None, tts=None):
    """텍스트도 오디오도 없는 요청: "입력을 보내달라"는 안내만 보내고 끝낸다."""
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    await session_repository.append_turn(session_id, input_pending_turn(input_meta, tts))
    snap = await session_repository.snapshot(session_id)

    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts))
    yield sse(input_required_event(session_id, "text_required", INPUT_REQUIRED_TEXT_MESSAGE))
    yield sse(done_event(session_id))


# ══════════════════════════════════════════════════════════════════════════
# [구획 5] respond_stream — 핵심 흐름 (이 서비스의 심장)
#
#   1. 세션(대화방) 확보 + 최근 대화 기록 로드
#   2. 안전검사 / 인지왜곡 분류 / 참고자료 검색 — 3가지를 동시에 실행
#   3. 분류 결과로 응답 정책 결정 (policy.resolve)
#   4. 위기 발화면: LLM 을 부르지 않고 고정 위기 메시지 + 핫라인 출력 후 종료
#   5. 평상시: 참고자료 정렬 → 프롬프트 구성 → LLM 답변을 글자 단위로 스트리밍
#   6. (옵션) 답변을 음성으로 합성 → 대화 기록 저장 → done
# ══════════════════════════════════════════════════════════════════════════

async def respond_stream(text: str, session_id=None, input_meta=None, tts=None, llm=None):
    # 1) 세션 확보 + 최근 대화 로드 (LLM 이 맥락을 이어가도록 이전 발화들을 가져온다)
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    prior_messages = await session_repository.recent_llm_messages(session_id)
    input_meta = default_text_input_meta(input_meta)

    # 2) 세 가지 분석을 "동시에" 실행 — gather 는 병렬 실행 후 셋 다 끝나면 결과를 준다.
    #    순서대로 하면 3번 기다려야 할 것을 1번 기다리는 시간으로 줄이는 것.
    safety, cls, cands = await asyncio.gather(
        services.safety.check(text),        # 위험(자살/자해) 발화인지
        services.classifier.classify_one(text),   # 인지왜곡 12분류 중 무엇인지
        services.retriever.retrieve(text),  # 관련 상담기법 자료 검색(RAG)
    )
    primary = cls["primary"]  # 대표 라벨 (예: "흑백 사고")
    # 대표 라벨의 확신 점수를 찾는다 (라벨 목록에서 primary 와 같은 항목의 score)
    confidence = max((l["score"] for l in cls["labels"] if l["label"] == primary), default=0.0)

    # 3) 이번 턴을 어떻게 응답할지 정책 결정 — 규칙은 respond/policy.py [구획 1]에서 편집
    policy = respond_policy.resolve(safety, cls)

    # 사용자 발화를 대화 기록에 저장하고, 분류 결과를 meta 이벤트로 프론트에 먼저 알린다
    await session_repository.append_turn(session_id, user_turn(text, primary, safety, input_meta, tts))
    snap = await session_repository.snapshot(session_id)
    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts, cls))

    # 4) 위기 분기: LLM 답변 생성 없이 고정 메시지 + 상담 핫라인을 즉시 출력하고 종료
    #    프론트가 metadata.region 을 보냈고 지역 연락처 DB 가 켜져 있으면 지역 창구를 앞에 붙인다
    if policy.is_crisis:
        region = (input_meta.get("metadata") or {}).get("region")
        payload = await respond_policy.crisis_payload(reason=safety.get("reason"), region=region)
        yield sse(payload)
        await session_repository.append_turn(session_id, crisis_turn(payload))
        if tts and tts.get("enabled"):
            yield sse(tts_event(session_id, await services.speech.synthesize_tts(payload.get("message", ""), tts)))
        yield sse(done_event(session_id))
        return

    # 5) 참고자료 정렬([구획 3]) → 프롬프트 구성(policy [구획 2]) → LLM 스트리밍
    #    정책이 RAG 를 끄면(chunks=[]) 참고자료 없이 답변한다
    #    cls_labels: multi_label 모델의 selected 판정을 가산점 조건으로 쓸 수 있게 전달
    chunks = rerank(cands, primary, confidence, top_n=policy.rag_top_n,
                    cls_labels=cls["labels"]) if policy.use_rag else []
    yield sse(chunks_event(session_id, chunks))

    # 시스템 프롬프트(상담 스타일·라벨 지침) + 이전 대화 + 이번 발화 → LLM 입력 메시지
    messages = respond_policy.build_llm_messages(policy.prompt_strategy, primary, chunks,
                                                 prior_messages, text)
    assistant_parts: list[str] = []
    # LLM 이 글자를 생성하는 대로 token 이벤트로 즉시 내보낸다 (타자 치듯 보이는 효과)
    async for tok in services.llm.chat_stream_async(messages, llm):
        assistant_parts.append(tok)
        yield sse(token_event(session_id, tok))

    # 조각들을 합쳐 완성된 답변을 만들고, 어떤 정책·확신으로 생성했는지와 함께 저장
    # (confidence 를 남겨야 운영 후 "저확신 강등이 몇 번 일어났나"를 DB 에서 집계할 수 있다)
    assistant_text = "".join(assistant_parts).strip()
    if assistant_text:
        policy_meta = {**policy.as_metadata(), "confidence": round(confidence, 4)}
        await session_repository.append_turn(
            session_id, assistant_turn(assistant_text, primary, chunks, policy=policy_meta))

    # 6) (옵션) 음성 합성 — 문장이 완성된 뒤에 해야 자연스러워서 스트리밍이 끝난 후 수행
    if tts and tts.get("enabled"):
        yield sse(tts_event(session_id, await services.speech.synthesize_tts(assistant_text, tts)))

    yield sse(done_event(session_id))
