"""[상담 흐름 — 기계장치] 상담 한 턴이 "어떻게" 흘러가는지가 전부 이 파일에 있다.

"무엇을 답할지"(정책·프롬프트·위기 문구)는 respond/policy.py — 이 파일은 순서와 배관만.

구획 목차 (Ctrl+F 로 "[구획" 검색):
    [구획 1] SSE 이벤트     프론트로 내보내는 메시지 조각들의 형식 + 단계 진행 신호(progress)
    [구획 2] 요청 정리      "텍스트/음성/이미지 중 뭐가 왔나" 판단 (RespondRequestContext)
    [구획 3] RAG 선별       검색 결과 중복 제거 후 상위 top_n 개만 고르기
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
import logging
from dataclasses import dataclass
from typing import Any

from .. import settings
from ..services import services
from ..session import (
    assistant_turn, crisis_turn, input_pending_turn, ocr_failed_turn,
    session_repository, stt_failed_turn, user_turn,
)
from . import context_merge
from . import policy as respond_policy

logger = logging.getLogger(__name__)

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
               cls: dict | None = None, analysis: dict | None = None) -> dict:
    """턴 시작 정보: 몇 번째 턴인지 + 인지왜곡 분류 결과(primary/labels).

    analysis: 이번 턴 분류가 "어떻게" 나왔는지의 관측 필드 (실험·디버깅용, 추가 계약):
        context_merged    선행 필터가 병합문을 분류 입력으로 골랐는가
        merge_trigger     병합을 발동시킨 트리거 ("prev_insufficient" | "short_utterance") 또는 None
        merge_rejected_by 병합을 포기한 이유 ("novelty" = 화제 전환 감지) 또는 None
        ladder_step       이번 턴 포함 연속 '불충분' 횟수 (0 = 불충분 아님)
    """
    payload = {"type": "meta", "session_id": session_id, "turn_count": turn_count,
               "input": input_meta, "tts": tts}
    if cls:
        payload.update({"primary": cls["primary"], "mode": cls["mode"], "labels": cls["labels"]})
    if analysis is not None:
        payload["analysis"] = analysis
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


# ── 단계 진행 신호(progress) ──────────────────────────────────────────────
# 프론트엔드가 "지금 어디까지 왔는지" 로딩 UI(체크리스트/진행바)를 그릴 수 있도록,
# 파이프라인의 각 단계가 "끝날 때마다" progress 이벤트를 하나씩 내보낸다.
#
# 단계 이름(stage)과 순서 — 요청 형태에 따라 앞뒤가 붙거나 빠진다:
#   extract   입력 변환: 음성(STT)·이미지(OCR) → 텍스트.  음성/이미지 입력일 때만.
#   input     상담 문장 접수: 처리할 텍스트가 확정되고 세션(대화방)이 준비됨.
#   analyze   동시 분석: 안전 점검 + 인지왜곡 분류 + 자료 검색(gather 3종) 완료.
#   route     응답 방침 결정: 정책 라우팅 + (평상시) 프롬프트 구성까지 완료.
#   generate  답변 생성: LLM 스트리밍 종료. SSE 에서는 생성과 동시에 token 으로
#             전송되므로 "생성 완료 = 텍스트 송신 완료"다 (별도 송신 단계 없음).
#             위기(crisis) 분기는 LLM 을 부르지 않으므로 이 단계가 없다.
#   speak     음성 합성: TTS 완료.  요청에 tts.enabled=true 일 때만.
#
# 이벤트 모양: {"type":"progress","session_id":...,"stage":"analyze",
#               "seq":2,"total":4,"label":"동시 분석(...) 완료"}
#   seq/total = 이 요청이 거칠 전체 단계 중 몇 번째가 끝났는지 (진행바용).
#   ※ 위기 분기가 확정되면 generate 단계가 사라지므로 route 이벤트부터 total 이
#     1 줄어든다. 프론트는 crisis 이벤트를 받으면 어차피 위기 화면으로 전환하므로
#     실용상 문제가 없다 (API_CONTRACT 7장 참고).
#   ※ STT/OCR 실패·입력 없음 경로는 진행할 단계가 없으므로 progress 를 보내지
#     않고 기존 실패 이벤트(input_required)로 끝난다.
# ──────────────────────────────────────────────────────────────────────────

# 단계별 사용자 안내 문구 — 프론트가 그대로 표시해도 되고 stage 로 직접 분기해도 된다.
STAGE_LABELS = {
    "extract": "입력 변환(음성·이미지 → 텍스트) 완료",
    "input": "상담 문장 접수 완료",
    "analyze": "동시 분석(안전 점검·왜곡 분류·자료 검색) 완료",
    "route": "응답 방침 결정·프롬프트 구성 완료",
    "generate": "상담 답변 생성 완료",
    "speak": "음성 합성 완료",
}


def stage_plan(extracted: bool, tts: dict | None, crisis: bool = False) -> list[str]:
    """이 요청이 거칠 단계 목록을 순서대로 만든다 — progress 의 seq/total 근거.

    extracted: 이 게이트웨이 안에서 STT/OCR 변환을 거쳤는가 (진입 스트림이 True 로 넘김.
               프론트가 전사문(stt.transcript)을 직접 보낸 경우는 변환이 없으므로 False).
    crisis:    위기 분기 확정 후 True — generate(LLM) 단계가 계획에서 빠진다.
    """
    plan = (["extract"] if extracted else []) + ["input", "analyze", "route"]
    if not crisis:
        plan.append("generate")
    if tts and tts.get("enabled"):
        plan.append("speak")
    return plan


def progress_event(session_id: str, stage: str, plan: list[str]) -> dict:
    """단계 하나가 '끝났다'는 신호 한 건을 만든다 (계획 안에서의 위치 포함)."""
    return {"type": "progress", "session_id": session_id, "stage": stage,
            "seq": plan.index(stage) + 1, "total": len(plan),
            "label": STAGE_LABELS.get(stage, stage)}


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
# [구획 3] RAG 선별 — 검색 결과에서 "프롬프트에 넣을 만큼"만 고른다
#
# 하는 일 2가지:
#   1. 중복 제거 — 같은 id 문서가 여러 번 오면 점수 높은 쪽만 남긴다
#   2. 점수 내림차순 상위 top_n 개만 반환 (순서 = 검색엔진 점수 그대로)
#
# ※ 과거에는 여기에 "라벨 일치 문서 가산점"(rerank)이 있었다 — 2026-07 제거.
#   근거: 실제 RAG 코퍼스(82청크) 전수 분석 결과 가산점이 참조하는 라벨 필드
#   (metadata.distortions)가 전 청크에서 비어 있어 한 번도 발동할 수 없었고(죽은
#   코드), 코퍼스의 분류축(해석편향·정신화·DBT/MI 등)이 분류기 12라벨과 달라
#   태깅 투자 가치도 낮았다. 라벨→기법 연결은 프롬프트(policy.py [구획 2]
#   LABEL_GUIDANCE)가 담당한다 — 검색은 발화 내용만으로 충분하다.
# ══════════════════════════════════════════════════════════════════════════

def select_chunks(candidates: list[dict], top_n: int | None = None) -> list[dict]:
    """검색 후보를 중복 제거하고 점수 상위 top_n 개만 돌려준다."""
    top_n = top_n or settings.RAG_TOP_N
    if not candidates:
        return []
    deduped: dict[str, dict] = {}
    for candidate in candidates:
        cid = candidate.get("id")
        score = float(candidate.get("score", 0.0))
        # 같은 id 문서가 여러 번 오면 점수가 높은 쪽만 남긴다
        if cid not in deduped or score > float(deduped[cid].get("score", 0.0)):
            deduped[cid] = candidate
    return sorted(deduped.values(), key=lambda c: float(c.get("score", 0.0)), reverse=True)[:top_n]


# ══════════════════════════════════════════════════════════════════════════
# [구획 4] 입력 형태별 진입 스트림 — 성공하면 전부 [구획 5] respond_stream 으로 합류
# ══════════════════════════════════════════════════════════════════════════

async def stt_then_respond_stream(session_id=None, input_meta=None, tts=None, llm=None,
                                  user_id: str | None = None):
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
        await session_repository.append_turn(session_id, stt_failed_turn(context.input_meta, result, tts), user_id=user_id)
        yield sse(stt_result_event(session_id, result))
        yield sse(input_required_event(session_id, result.get("status") or "stt_failed",
                                       INPUT_REQUIRED_STT_MESSAGE))
        yield sse(done_event(session_id))
        return

    # STT 성공: 전사문을 반영한 컨텍스트로 바꾸고 일반 상담 흐름을 이어서 실행
    context = context.with_transcript(result)
    yield sse(stt_result_event(session_id, result))
    # [progress] 1단계(extract) 완료 신고: 음성 → 텍스트 변환이 끝났다.
    # 이후 단계(input~)는 respond_stream 이 이어서 신고한다 — extracted=True 를 넘겨
    # 거기서도 같은 단계 계획(총 단계 수)으로 seq/total 을 계산하게 한다.
    yield sse(progress_event(session_id, "extract", stage_plan(extracted=True, tts=tts)))
    async for event in respond_stream(context.text or "", session_id, context.input_meta, tts, llm,
                                      extracted=True, user_id=user_id):
        yield event  # respond_stream 이 내보내는 이벤트를 그대로 통과시킨다


async def ocr_then_respond_stream(session_id=None, input_meta=None, tts=None, llm=None,
                                  user_id: str | None = None):
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
        await session_repository.append_turn(session_id, ocr_failed_turn(stored_meta, result, tts), user_id=user_id)
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
    # [progress] 1단계(extract) 완료 신고: 이미지 → 텍스트(OCR) 변환이 끝났다 (STT 흐름과 대칭).
    yield sse(progress_event(session_id, "extract", stage_plan(extracted=True, tts=tts)))
    async for event in respond_stream(user_text, session_id, stored_meta, tts, llm,
                                      extracted=True, user_id=user_id):
        yield event


async def input_pending_stream(session_id=None, input_meta=None, tts=None, user_id=None):
    """텍스트도 오디오도 없는 요청: "입력을 보내달라"는 안내만 보내고 끝낸다."""
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    input_meta = input_meta or {}
    await session_repository.append_turn(session_id, input_pending_turn(input_meta, tts), user_id=user_id)
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

async def respond_stream(text: str, session_id=None, input_meta=None, tts=None, llm=None,
                         extracted: bool = False, user_id: str | None = None):
    """상담 한 턴의 핵심 흐름.

    extracted: 앞에서 STT/OCR 변환(extract 단계)을 이미 거쳤다는 뜻 — progress 신호의
               단계 계획(seq/total)에 그 단계를 포함시킨다.
    user_id:   요청자 식별자 (api/v1.py current_user — 가상 ID 또는 Entra oid).
               위기 분기에서 프로필에 저장된 지역(설문 location)으로 핫라인을 찾을 때 쓴다.
    """
    # 1) 세션 확보 + 최근 대화 로드 (LLM 이 맥락을 이어가도록 이전 발화들을 가져온다)
    session = await session_repository.ensure(session_id)
    session_id = session["session_id"]
    prior_messages = await session_repository.recent_llm_messages(session_id)
    input_meta = default_text_input_meta(input_meta)

    # [progress] 이 요청이 거칠 단계 계획을 세운다 (위기 여부는 분석 전이라 아직 모름 —
    # 평상시 기준. 위기로 확정되면 아래 4)에서 위기 경로 기준으로 다시 계산한다).
    # "상담 문장 접수" 단계 완료: 텍스트가 확정됐고 대화방이 준비됐다.
    plan = stage_plan(extracted, tts)
    yield sse(progress_event(session_id, "input", plan))

    # 2) [선행 필터] 분류기를 부르기 "전에", 단독 발화를 분류할지 병합문을 분류할지
    #    미리 결정한다 — 세션은 이미 로드돼 있어(1번) 직전 라벨 확인은 비용 0 이고,
    #    novelty 게이트는 순수 문자열 규칙이라 모델 없이 돈다. 그 결과 분류기 호출은
    #    어떤 턴에서도 정확히 1회다 (이전 twopass 는 불충분 턴마다 2회 → CPU 병목).
    #    트리거: ① 직전 사용자 턴이 '불충분' (clarify 재발화 — 수렴 케이스)
    #            ② 현재 발화가 단문(≤SHORT_CHARS) — 불충분의 길이 프록시. 파편이 처음
    #               나온 턴(직전=확신 왜곡)을 ①이 못 잡는 구멍을 메운다
    #    병합 조건: 트리거 발동 + novelty 게이트 통과("실제 포함된 맥락" 기준 —
    #    화제 전환이면 병합 포기, 직전 왜곡으로 끌려가는 오염 방지).
    #    analysis = 이 과정의 관측 기록 (meta 이벤트·세션 저장으로 나감).
    analysis = {"context_merged": False, "merge_rejected_by": None,
                "merge_trigger": None, "ladder_step": 0}
    classify_input = text
    if settings.CLASSIFY_PREMERGE and settings.CLASSIFY_CONTEXT_MAX_TURNS > 0:
        turns_hist = session.get("turns") or []
        window = context_merge.recent_user_texts(turns_hist)
        if window:
            trigger = None
            if context_merge.last_user_label(turns_hist) == "불충분":
                trigger = "prev_insufficient"
            elif 0 < settings.CLASSIFY_PREMERGE_SHORT_CHARS >= len(text.strip()):
                trigger = "short_utterance"
            if trigger:
                merged, included = context_merge.merge_candidate(
                    window, text, settings.CLASSIFY_CONTEXT_MAX_TURNS,
                    settings.CLASSIFY_CONTEXT_MAX_CHARS)
                if merged != text:
                    if context_merge.novelty(text, included):
                        analysis["merge_rejected_by"] = "novelty"  # 화제 전환 — 단독 분류 유지
                    else:
                        classify_input = merged
                        analysis["context_merged"] = True
                        analysis["merge_trigger"] = trigger

    # 3) 세 가지 분석을 "동시에" 실행 — gather 는 병렬 실행 후 셋 다 끝나면 결과를 준다.
    #    safety 와 RAG 는 언제나 "현재 발화만" 본다 (안전 배리어는 매 턴 독립이어야 하고,
    #    검색은 이번 발화의 주제로 해야 맞다). 분류만 선행 필터가 고른 입력을 쓴다.
    safety, cls, cands = await asyncio.gather(
        services.safety.check(text),                       # 위험(자살/자해) 발화인지 — 항상 원문
        services.classifier.classify_one(classify_input),  # 12분류 — 단독문 또는 병합문, 1회
        services.retriever.retrieve(text),                 # 상담기법 검색(RAG) — 항상 원문
    )

    # [progress] "동시 분석" 단계 완료: 분류가 확정됐다 (선행 필터가 고른 입력 기준).
    yield sse(progress_event(session_id, "analyze", plan))
    primary = cls["primary"]  # 대표 라벨 (예: "흑백 사고") — 병합문을 분류했다면 그 결과
    # 대표 라벨의 확신 점수를 찾는다 (라벨 목록에서 primary 와 같은 항목의 score)
    confidence = max((l["score"] for l in cls["labels"] if l["label"] == primary), default=0.0)

    # 4) [완화 사다리 단계] 최종 라벨이 '불충분'이면 "몇 번째 연속 불충분인지"를 센다.
    #    이 숫자로 policy.resolve 가 질문의 각도·무게를 바꾸고, ESCAPE_AFTER(기본 4)째는
    #    질문을 멈추는 수용·동행 모드로 전환한다 (4연속 ≈ 발화 회피 신호라는 실측 근거).
    #    INSUFFICIENT_ESCAPE_AFTER=0 은 사다리 전체 끔 (0=꺼짐 관례 — POLICY_MIN_CONFIDENCE 와 동일).
    if primary == "불충분" and settings.INSUFFICIENT_ESCAPE_AFTER > 0:
        analysis["ladder_step"] = context_merge.trailing_insufficient(session.get("turns") or []) + 1

    # 5) 이번 턴을 어떻게 응답할지 정책 결정 — 규칙은 respond/policy.py [구획 1]에서 편집
    policy = respond_policy.resolve(safety, cls, ladder_step=analysis["ladder_step"])

    # 사용자 발화를 대화 기록에 저장하고, 분류 결과를 meta 이벤트로 프론트에 먼저 알린다
    # (저장하는 primary 는 병합 분류까지 끝난 "최종" 라벨 — 다음 턴의 선행 필터·사다리가
    #  이 값을 본다. analysis 를 사용자 턴에도 남겨야 "병합으로 얻은 라벨"을 원발화 라벨과
    #  구분해 집계·재학습 추출에서 걸러낼 수 있다 — 위기·빈 답변 턴도 기록이 남도록 여기서 저장)
    # 멀티라벨 선택 결과 전체를 세션에 남긴다 — 학습 데이터가 최대 4개 동시 라벨까지
    # 포함하므로 primary 만 저장하면 동시 왜곡 정보가 유실된다 (재현 지적, 2026-07-04)
    selected_labels = [{"label": l["label"], "score": round(float(l.get("score", 0.0)), 4)}
                       for l in sorted(cls["labels"], key=lambda l: -float(l.get("score", 0.0)))
                       if l.get("selected")]
    await session_repository.append_turn(
        session_id, user_turn(text, primary, safety, input_meta, tts,
                              analysis=analysis, selected_labels=selected_labels),
        user_id=user_id)
    snap = await session_repository.snapshot(session_id)
    yield sse(meta_event(session_id, snap["turn_count"], input_meta, tts, cls, analysis))

    # 4) 위기 분기: LLM 답변 생성 없이 고정 메시지 + 상담 핫라인을 즉시 출력하고 종료
    #    지역(시도)이 정해지고 지역 연락처 DB 가 켜져 있으면 지역 창구를 전국 공통 앞에 붙인다.
    #    region 은 metadata.region(프론트 명시) → 프로필(설문 저장 지역) 순으로 정한다.
    #    user_id 배선 완료: 가상 ID/oid 가 route → 여기까지 전달돼 프로필 경로가 살아 있다.
    #    resolve_region 은 프로필 조회(DB) 때문에 블로킹일 수 있어 to_thread 로 오프로딩.
    if policy.is_crisis:
        # [progress] 위기 확정 — LLM 생성(generate) 단계가 빠진 위기 경로 계획으로
        # 갱신하고 "방침 결정" 완료를 신고한다. (여기서부터 total 이 1 줄어드는 이유 —
        # 프론트는 곧이어 오는 crisis 이벤트로 위기 화면으로 전환하므로 실용상 무해)
        plan = stage_plan(extracted, tts, crisis=True)
        yield sse(progress_event(session_id, "route", plan))
        region, district = await asyncio.to_thread(
            respond_policy.resolve_region, input_meta, user_id)
        payload = await respond_policy.crisis_payload(
            reason=safety.get("reason"), region=region, district=district)
        yield sse(payload)
        await session_repository.append_turn(session_id, crisis_turn(payload), user_id=user_id)
        if tts and tts.get("enabled"):
            yield sse(tts_event(session_id, await services.speech.synthesize_tts(payload.get("message", ""), tts)))
            # [progress] "음성 합성" 단계 완료 (위기 안내문의 TTS).
            yield sse(progress_event(session_id, "speak", plan))
        yield sse(done_event(session_id))
        return

    # 5) 참고자료 선별([구획 3]) → 프롬프트 구성(policy [구획 2]) → LLM 스트리밍
    #    정책이 RAG 를 끄면(chunks=[]) 참고자료 없이 답변한다
    chunks = select_chunks(cands, top_n=policy.rag_top_n) if policy.use_rag else []
    yield sse(chunks_event(session_id, chunks))

    # [멀티라벨 보조 지침] 분류기가 primary 와 "함께 선택한"(selected=True) 부차 왜곡들.
    # threshold(0.55) 이상 왜곡이 여러 개면 주 지침 하나만으로는 관찰된 패턴을 다 못
    # 담으므로, score 순으로 상한(LABEL_GUIDANCE_MAX-1)까지 보조 지침으로 병기한다.
    # 정상/불충분은 selection_policy 가 배타 처리하므로 여기 걸릴 일이 없지만 한 번 더 거른다.
    secondary_labels: list[str] = []
    if policy.prompt_strategy == "cbt_label_guided" and settings.LABEL_GUIDANCE_MAX > 1:
        secondary_labels = [l["label"] for l in
                            sorted(cls["labels"], key=lambda l: -float(l.get("score", 0.0)))
                            if l.get("selected") and l["label"] not in ("정상", "불충분", primary)
                            ][: settings.LABEL_GUIDANCE_MAX - 1]

    # 시스템 프롬프트(상담 스타일·라벨 지침) + 이전 대화 + 이번 발화 → LLM 입력 메시지
    messages = respond_policy.build_llm_messages(policy.prompt_strategy, primary, chunks,
                                                 prior_messages, text,
                                                 secondary_labels=secondary_labels)
    # [progress] "응답 방침 결정" 단계 완료: 정책 라우팅 + 프롬프트 구성까지 끝났다.
    # 다음 이벤트부터 LLM 답변 조각(token)이 흘러온다 — 프론트는 여기서 로딩 표시를
    # "답변 작성 중"으로 바꾸면 자연스럽다.
    yield sse(progress_event(session_id, "route", plan))
    assistant_parts: list[str] = []
    # LLM 이 글자를 생성하는 대로 token 이벤트로 즉시 내보낸다 (타자 치듯 보이는 효과)
    async for tok in services.llm.chat_stream_async(messages, llm):
        assistant_parts.append(tok)
        yield sse(token_event(session_id, tok))
    # [progress] "답변 생성" 단계 완료: 스트리밍이라 생성 = 전송이므로, 이 신호가
    # 곧 "텍스트 송신 완료"이기도 하다 (별도 송신 단계를 두지 않는 이유).
    yield sse(progress_event(session_id, "generate", plan))

    # 조각들을 합쳐 완성된 답변을 만들고, 어떤 정책·확신으로 생성했는지와 함께 저장
    # (confidence 를 남겨야 운영 후 "저확신 강등이 몇 번 일어났나"를 DB 에서 집계할 수 있다)
    assistant_text = "".join(assistant_parts).strip()
    if assistant_text:
        # analysis(병합·사다리 관측 필드)도 함께 저장 — 운영 후 "병합이 몇 번 발동했고
        # 사다리가 어디까지 갔나"를 세션 DB 에서 집계하기 위해서다.
        # secondary_labels 는 보조 지침이 실제로 주입된 턴에만 남긴다 (있을 때만 기록).
        policy_meta = {**policy.as_metadata(), "confidence": round(confidence, 4), **analysis}
        if secondary_labels:
            policy_meta["secondary_labels"] = secondary_labels
        await session_repository.append_turn(
            session_id, assistant_turn(assistant_text, primary, chunks, policy=policy_meta),
            user_id=user_id)

    # 6) (옵션) 음성 합성 — 문장이 완성된 뒤에 해야 자연스러워서 스트리밍이 끝난 후 수행
    if tts and tts.get("enabled"):
        yield sse(tts_event(session_id, await services.speech.synthesize_tts(assistant_text, tts)))
        # [progress] "음성 합성" 단계 완료 — 마지막 단계라 이 직후 done 이 온다.
        yield sse(progress_event(session_id, "speak", plan))

    yield sse(done_event(session_id))
