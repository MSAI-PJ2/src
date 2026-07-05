"""[취준 기능] 채용공고 적합도 분석 + 경험 기반 자소서 초안 — 전부 이 한 파일에 있다.

마음갈피의 확장 기능. CBT 상담과의 연결고리가 핵심이다:
    "난 어디에도 못 붙을 거야"(파국화/과잉일반화) 같은 인지왜곡에 대해,
    실제 공고 분석 결과("요건 8개 중 5개 충족")라는 **사실 근거**를 만들어 준다.
    → CBT 에서 말하는 '행동 실험(근거 수집)'을 앱 안에서 바로 하는 셈.

제공 주소 (전부 /v1 아래, 기존 인증 그대로 적용):
    POST /v1/career/profile       내 스펙/경험 저장 (서버 메모리 — 데모용)
    GET  /v1/career/profile/{id}  저장한 프로필 조회
    POST /v1/career/analyze       공고 붙여넣기 → 공고 분석(요약·키워드) + 적합도 분석 (JSON)
    POST /v1/career/cover-letter  공고+문항 → 자소서 초안 (SSE 스트리밍, /v1/respond 와 같은 형식)
    POST /v1/career/review        내가 쓴 자소서/이력서 첨삭 — 강점/수정제안/수정본 (SSE 스트리밍)
    POST /v1/career/resume        프로필 → 공고 맞춤 이력서 구조 초안 (JSON)
    POST /v1/career/search        워크넷(고용24) 공식 API 로 공고 검색 (WORKNET_API_KEY 필요)
    POST /v1/career/analyze-batch 공고 여러 개 → 각각 분석 후 적합도순 정렬 (JSON)

새 환경변수 없음 — 기존 AZURE_OPENAI_* 를 그대로 쓴다 (services.llm 재사용).

AI 윤리 주의(발표 때 그대로 말할 것):
    - 자소서는 "완성본"이 아니라 "초안"이다. 프롬프트에 "경험에 없는 사실을
      지어내지 말 것"을 강제했고, 초안 끝에 [어떤 경험을 근거로 썼는지] 매핑을
      붙여서 사용자가 검증할 수 있게 했다 (대필이 아니라 작성 보조).
    - 적합도 분석은 "판정"이 아니라 "참고 정보"다. 응답에 항상 그 한계를 명시한다.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .services import services
from .respond.flow import sse, token_event, done_event  # SSE 형식을 상담 쪽과 통일


# ══════════════════════════════════════════════════════════════════════════
# [구획 1] 요청 모델 — 프론트가 보내는 JSON 의 모양
# ══════════════════════════════════════════════════════════════════════════

class Experience(BaseModel):
    """경험 한 개. 예: title="교내 해커톤 대상", detail="4인 팀 백엔드 담당, FastAPI로..."."""
    title: str
    detail: str = ""


class ProfileIn(BaseModel):
    """사용자의 스펙/경험 묶음. analyze/cover-letter 에 인라인으로 넣거나, 먼저 저장해두고 profile_id 로 참조."""
    profile_id: str | None = None       # 생략하면 서버가 발급
    education: str | None = None        # 예: "OO대 컴퓨터공학 4학년"
    skills: list[str] = Field(default_factory=list)   # 예: ["Python", "Azure", "SQL"]
    experiences: list[Experience] = Field(default_factory=list)
    target_role: str | None = None      # 예: "백엔드 개발자"


class AnalyzeIn(BaseModel):
    """POST /v1/career/analyze 의 입력 — 공고 원문을 그대로 복붙해서 보낸다."""
    posting: str                        # 채용공고 전문 (복붙)
    profile: ProfileIn | None = None    # 인라인 프로필 (있으면 이것 우선)
    profile_id: str | None = None       # 저장해둔 프로필 참조
    llm: dict[str, Any] | None = None   # {"temperature":..., "max_completion_tokens":...}


class CoverLetterIn(BaseModel):
    """POST /v1/career/cover-letter 의 입력."""
    posting: str                        # 채용공고 전문
    question: str = "지원 동기와 입사 후 포부를 기술하시오."  # 자소서 문항
    max_chars: int = 1000               # 문항 글자수 제한 (한국 자소서 관행)
    profile: ProfileIn | None = None
    profile_id: str | None = None
    llm: dict[str, Any] | None = None


class ReviewIn(BaseModel):
    """POST /v1/career/review 의 입력 — 사용자가 쓴 자소서(또는 이력서)를 첨삭받는다."""
    draft: str                          # 사용자가 쓴 원문
    doc_type: str = "cover_letter"      # "cover_letter"(자소서) | "resume"(이력서)
    posting: str | None = None          # 공고를 주면 "공고 맞춤" 첨삭 (없어도 동작)
    question: str | None = None         # 자소서 문항 (있으면 문항 부합도까지 점검)
    max_chars: int = 1000               # 글자수 제한 — 수정본이 이 안에 들어오게
    profile: ProfileIn | None = None    # 있으면 "경험에 없는 내용" 검증에 사용
    profile_id: str | None = None
    llm: dict[str, Any] | None = None


class SearchIn(BaseModel):
    """POST /v1/career/search — 워크넷(고용24) 공식 오픈 API 로 공고를 검색한다.

    사람인 등 민간 사이트 크롤링은 약관 위반·차단 리스크가 있어 쓰지 않고,
    정부 공식 API 를 쓴다. 환경변수 WORKNET_API_KEY 필요
    (work24.go.kr → 오픈API → 인증키 신청, 무료).
    """
    keyword: str                        # 검색어 (예: "데이터 분석")
    region: str | None = None           # 지역 코드/명 (API 스펙에 따름, 선택)
    count: int = 5                      # 가져올 공고 수 (1~10)


class AnalyzeBatchIn(BaseModel):
    """POST /v1/career/analyze-batch — 공고 여러 개를 한 번에 분석해 적합도순으로 돌려준다."""
    postings: list[str]                 # 공고 원문 목록 (2~10개)
    profile: ProfileIn | None = None
    profile_id: str | None = None
    llm: dict[str, Any] | None = None


class ResumeIn(BaseModel):
    """POST /v1/career/resume 의 입력 — 프로필을 공고에 맞춘 이력서 구조로 변환."""
    posting: str | None = None          # 공고를 주면 그 직무에 맞게 강조점을 조정
    profile: ProfileIn | None = None
    profile_id: str | None = None
    llm: dict[str, Any] | None = None


# ══════════════════════════════════════════════════════════════════════════
# [구획 2] 프로필 저장소 — 데모용 서버 메모리 (재시작하면 사라짐)
#
# 운영으로 가면 세션과 같은 Cosmos DB 컨테이너로 옮기면 된다 (session.py 참고).
# 지금은 "프론트가 매번 프로필 전체를 다시 보내지 않아도 되게" 하는 편의 기능.
# ══════════════════════════════════════════════════════════════════════════

_profiles: dict[str, dict] = {}


def _resolve_profile(inline: ProfileIn | None, profile_id: str | None) -> dict:
    """인라인 프로필이 있으면 그것을, 없으면 저장소에서 찾아서 dict 로 돌려준다."""
    if inline is not None:
        return inline.model_dump(exclude_none=True)
    if profile_id and profile_id in _profiles:
        return _profiles[profile_id]
    raise HTTPException(400, "profile 을 함께 보내거나, 저장된 profile_id 를 지정하세요.")


def _profile_text(profile: dict) -> str:
    """프로필 dict 를 LLM 프롬프트에 넣을 읽기 좋은 텍스트로 바꾼다."""
    lines: list[str] = []
    if profile.get("education"):
        lines.append(f"학력/전공: {profile['education']}")
    if profile.get("target_role"):
        lines.append(f"희망 직무: {profile['target_role']}")
    if profile.get("skills"):
        lines.append("보유 기술: " + ", ".join(profile["skills"]))
    for i, exp in enumerate(profile.get("experiences", []), 1):
        title = exp.get("title", "") if isinstance(exp, dict) else exp.title
        detail = exp.get("detail", "") if isinstance(exp, dict) else exp.detail
        lines.append(f"경험{i}: {title} — {detail}")
    return "\n".join(lines) or "(입력된 프로필 없음)"


# ══════════════════════════════════════════════════════════════════════════
# [구획 3] 프롬프트 — "무엇을 답할지" (사람이 편집하는 부분)
# ══════════════════════════════════════════════════════════════════════════

ANALYZE_SYSTEM = """당신은 취업 준비생을 돕는 커리어 분석 도우미입니다.
채용공고와 지원자 프로필을 비교해 **사실에 근거한** 적합도 분석을 합니다.

반드시 지킬 것:
1. 프로필에 없는 능력/경험을 있다고 가정하지 마세요.
0. 응답의 첫 글자는 반드시 { 이고 마지막 글자는 } 입니다. JSON 앞뒤에 어떤 설명·인사·코드펜스도 붙이지 마세요.
2. 낙관도 비관도 아닌, 근거 있는 문장만 쓰세요. (충족/부족을 공고의 문구와 프로필의 문구로 짝지어 설명)
3. 응답은 아래 JSON 형식으로만 하세요. 코드블록(```)이나 다른 텍스트를 붙이지 마세요.\n4. 간결하게: matched/gaps 는 각각 최대 4개, 각 문장은 한 줄로 짧게. (응답이 잘리지 않게)

{
  "job_summary": {
    "role": "공고가 뽑는 직무 한 줄",
    "main_tasks": [ "핵심 업무 3~5개 (공고 문구 기반)" ],
    "required": [ "필수 자격요건 목록" ],
    "preferred": [ "우대사항 목록" ]
  },
  "keywords": [ "이 공고에서 자소서/이력서에 반드시 녹여야 할 직무 키워드 5~10개" ],
  "fit_score": 0~100 사이 정수 (공고 요건 대비 프로필 충족 정도),
  "summary": "한 문장 요약",
  "matched": [ {"requirement": "공고의 요건 문구", "evidence": "프로필의 어떤 부분이 충족하는지"} ],
  "gaps": [ {"requirement": "부족한 요건", "how_to_fill": "현실적으로 보완하는 방법 1가지"} ],
  "recommendation": "지원 권장 | 보완 후 지원 | 다른 공고 탐색" 중 하나,
  "reframe_evidence": [ "인지왜곡('난 어디에도 안 될 거야' 등)에 반박할 수 있는 사실 문장 2~3개. 예: '이 공고의 필수 요건 6개 중 4개를 이미 충족하고 있다'" ],
  "caveat": "이 분석은 공고 원문과 입력된 프로필만을 근거로 한 참고 정보이며, 실제 합격 여부를 예측하지 않습니다."
}"""

REVIEW_SYSTEM = """당신은 자기소개서 첨삭 전문가입니다. 사용자가 직접 쓴 초안을 받아
더 좋게 다듬도록 돕습니다. 대신 써 주는 것이 아니라, 사용자의 글을 살려서 고칩니다.

반드시 지킬 것 (윤리 규칙 — 위반 금지):
1. 사용자의 초안과 프로필에 **없는 사실을 추가하지 마세요.** 수치·수상·직책 창작 금지.
2. 사용자의 목소리(문체·경험)를 유지하세요. 전면 재작성이 아니라 첨삭입니다.
3. 채용공고가 주어졌다면, 공고의 키워드가 자연스럽게 드러나도록 제안하되 억지로 끼워넣지 마세요.
4. 지적은 구체적으로: "이 문장이 왜 약한지 + 어떻게 바꾸면 좋은지"를 짝으로.

가장 중요한 규칙 — 사실 확인:
지원자의 실제 경험 목록이 주어진 경우, 초안에 그 목록에 없는 경력·인턴·수상·자격 주장이
있으면 반드시 [사실 확인 필요] 에서 해당 문장을 그대로 인용해 지적하고, [수정 제안] 에서
그 문장의 삭제 또는 실제 경험으로의 교체를 제안하세요. 목록에 없는 주장을 다듬거나
개선해서 유지하도록 제안하는 것은 금지입니다.

출력 형식 (아래 제목 그대로, 순서대로):
[총평] 2~3문장. 좋은 점 먼저, 그다음 가장 큰 개선 포인트 1가지.
[사실 확인 필요] 경험 목록에 없는 주장 (없으면 "없음" 이라고만 쓸 것).
[강점] 살려야 할 부분 2~3개 (초안의 실제 문장을 짚어서).
[수정 제안] 문장/단락별로 "원문 → 제안"을 3~6개. 각 제안에 이유 한 줄.
[주의] 전체 수정본은 제공하지 않습니다. 위 제안을 참고해 지원자 본인이 직접 고치세요.
최종 확인과 수정 책임은 지원자 본인에게 있습니다."""

REVIEW_RESUME_SYSTEM = """당신은 이력서 첨삭 전문가입니다. 사용자의 이력서 원문을 받아
채용공고(주어진 경우)에 맞게 다듬도록 돕습니다.

반드시 지킬 것 (윤리 규칙 — 위반 금지):
1. 원문과 프로필에 없는 사실(기간·수치·직책·자격증)을 추가하지 마세요.
2. "담당했다" 식 나열은 "무엇을 해서 어떤 결과"의 성과형 불릿으로 바꾸는 제안을 하세요.
3. 공고가 있으면 공고 키워드와 겹치는 항목을 상단 배치하도록 제안하세요.

출력 형식 (제목 그대로, 순서대로):
[총평] 2~3문장.
[수정 제안] 항목별 "원문 → 제안" 3~6개, 각 이유 한 줄.
[주의] 전체 수정본은 제공하지 않습니다. 위 제안을 참고해 지원자 본인이 직접 고치세요.
최종 확인 책임은 지원자 본인에게 있습니다."""

RESUME_SYSTEM = """당신은 이력서 작성 도우미입니다. 지원자 프로필을 받아
(공고가 있으면 그 직무에 맞게) 이력서 구조 초안을 만듭니다.

반드시 지킬 것:
1. 프로필에 **없는 사실을 만들지 마세요.** 기간·수치·직책을 모르면 비워두고 missing_info 에 적으세요.
2. 경험은 "무엇을 했다"가 아니라 "무엇을 해서 어떤 결과를 냈다" 형태의 불릿으로 바꾸세요 (프로필에 있는 내용 범위 안에서).
3. 공고가 있으면 공고 키워드와 겹치는 역량을 앞쪽에 배치하세요.
4. 응답은 아래 JSON 형식으로만. 코드블록(```) 금지.

{
  "headline": "이름 아래 들어갈 한 줄 소개 (직무 지향)",
  "summary": "2~3문장 요약 (프로필 사실만)",
  "core_competencies": [ "핵심 역량 키워드 4~6개" ],
  "experiences": [ {"title": "경험명", "bullets": [ "성과 중심 불릿 1~3개" ]} ],
  "skills": [ "기술 스택" ],
  "education": "학력 (입력된 대로)",
  "tailoring_notes": [ "공고 기준으로 이 이력서에서 강조한 점 (공고 없으면 빈 배열)" ],
  "missing_info": [ "지원자가 추가로 입력해야 완성되는 정보 (기간, 수치, 자격증 등)" ],
  "caveat": "이 이력서는 입력된 프로필만으로 만든 초안입니다. 사실 확인 후 사용하세요."
}"""

COVER_LETTER_SYSTEM = """당신은 취업 준비생의 자기소개서 작성을 돕는 도우미입니다.
채용공고, 자소서 문항, 지원자의 실제 경험 목록을 받아 **초안**을 작성합니다.

반드시 지킬 것 (윤리 규칙 — 위반 금지):
1. 지원자의 경험 목록에 **없는 사실을 절대 지어내지 마세요.** 수치·수상·직책을 창작하면 안 됩니다.
2. 경험이 부족해 문항을 채우기 어렵다면, 부족하다고 솔직히 알리고 어떤 경험을 추가 입력하면 좋을지 제안하세요.
3. 문체는 담백하고 구체적으로. 상투적 표현("귀사", "미래의 인재") 남발 금지.
4. 글자수 제한을 지키세요 (약간 미달은 괜찮고, 초과는 안 됩니다).

출력 형식:
먼저 자소서 초안 본문을 쓰고, 그 아래에 구분선(---)을 긋고 다음을 붙이세요:
[근거 매핑] 초안의 각 단락이 지원자의 어떤 경험(경험1, 경험2...)을 근거로 했는지 한 줄씩.
[다듬을 점] 지원자가 직접 보강해야 할 부분 1~3개.
※ 이 초안은 작성 보조용입니다. 반드시 본인이 검토·수정 후 사용하세요."""


# ══════════════════════════════════════════════════════════════════════════
# [구획 4] LLM 호출 도우미
# ══════════════════════════════════════════════════════════════════════════

async def _chat_collect(messages: list[dict], options: dict | None) -> str:
    """스트리밍 전용인 services.llm 을 재사용해서, 조각을 다 모아 한 문자열로 돌려준다.

    (분석 결과는 JSON 하나라 스트리밍이 의미 없음 — 다 모아서 파싱한다.)
    """
    parts: list[str] = []
    async for tok in services.llm.chat_stream_async(messages, options):
        parts.append(tok)
    return "".join(parts)


def _parse_json_or_raw(text: str) -> dict:
    """LLM 응답에서 JSON 오브젝트를 최대한 견고하게 뽑아낸다.

    모델이 규칙을 어기고 JSON 앞뒤에 설명 문구나 ``` 펜스를 붙이는 경우가 있어서,
    (1) 그대로 파싱 → (2) 본문에서 첫 '{' 를 찾아 그 지점부터 JSON 하나만 해석
    (raw_decode 는 JSON 이 끝나는 위치를 알아내므로 뒤에 잡담이 붙어도 무시된다)
    순서로 시도한다. 전부 실패하면 원문을 raw 로 감싸 돌려준다.
    """
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start != -1:
        try:
            obj, _end = json.JSONDecoder().raw_decode(cleaned[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    if start != -1:
        repaired = _repair_truncated_json(cleaned[start:])
        if repaired is not None:
            repaired["_repaired"] = "응답이 토큰 상한에서 잘려 마지막 항목이 유실됐을 수 있음"
            return repaired
    return {"raw": text, "parse_error": "LLM 응답에서 JSON 을 찾지 못했습니다."}


def _repair_truncated_json(s: str) -> dict | None:
    """토큰 상한 때문에 중간에 잘린 JSON 을 최대한 복구한다.

    방법: 뒤에서부터 조금씩 잘라가며 '마지막으로 완결된 값'까지만 남기고,
    열려 있는 문자열/괄호를 순서대로 닫아본다. 성공하면 dict, 실패하면 None.
    """
    for cut in range(len(s), max(len(s) - 2000, 0), -1):
        head = s[:cut].rstrip().rstrip(",")
        # 문자열 내부/괄호 상태 추적
        stack, in_str, esc = [], False, False
        for ch in head:
            if in_str:
                if esc: esc = False
                elif ch == "\\": esc = True
                elif ch == '"': in_str = False
            elif ch == '"': in_str = True
            elif ch in "{[": stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack and stack[-1] == ch: stack.pop()
                else: break
        if in_str:
            continue  # 문자열 한가운데서 끊긴 지점 — 더 잘라서 재시도
        candidate = head + "".join(reversed(stack))
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════
# [구획 5] 라우트
# ══════════════════════════════════════════════════════════════════════════

CAREER_VERSION = "5"  # 파일이 실제로 반영됐는지 /diag 로 확인하기 위한 표식

career = APIRouter(prefix="/career")


@career.get("/diag")
async def diag():
    """설정 진단 — Azure OpenAI 환경변수가 서버에 실제로 들어왔는지 확인 (값은 노출 안 함).

    500 이 나면 프론트/테스트가 제일 먼저 여기를 확인하면 된다.
    셋 중 하나라도 false 면: uvicorn 을 `--env-file .env` 옵션과 함께 재시작할 것.
    """
    import os
    return {
        "career_version": CAREER_VERSION,
        "AZURE_OPENAI_ENDPOINT": bool(os.getenv("AZURE_OPENAI_ENDPOINT")),
        "AZURE_OPENAI_API_KEY": bool(os.getenv("AZURE_OPENAI_API_KEY")),
        "AZURE_OPENAI_DEPLOYMENT": bool(os.getenv("AZURE_OPENAI_DEPLOYMENT")),
        "hint": "false 가 있으면 서버를 이렇게 재시작: uvicorn app.main:app --reload --port 8080 --env-file .env",
    }


async def _chat_collect_or_502(messages: list[dict], options: dict | None) -> str:
    """_chat_collect 를 감싸서, LLM 실패 시 원인 문장을 담은 502 로 바꿔준다.

    (감싸지 않으면 FastAPI 기본 동작으로 'Internal Server Error' 만 보여서 디버깅이 안 됨)
    """
    try:
        return await _chat_collect(messages, options)
    except Exception as exc:
        raise HTTPException(
            502,
            f"LLM 호출 실패: {exc} — GET /v1/career/diag 로 환경변수 상태를 확인하세요. "
            f"(로컬이면 uvicorn 재시작 시 --env-file .env 를 붙였는지 확인)",
        )


@career.post("/profile")
async def save_profile(body: ProfileIn):
    """내 스펙/경험을 저장한다. 이후 analyze/cover-letter 에서 profile_id 만 보내면 됨."""
    pid = body.profile_id or uuid.uuid4().hex[:12]
    data = body.model_dump(exclude_none=True)
    data["profile_id"] = pid
    _profiles[pid] = data
    return {"profile_id": pid, "saved": True,
            "experiences": len(data.get("experiences", []))}


@career.get("/profile/{profile_id}")
async def get_profile(profile_id: str):
    """저장한 프로필 조회. 없으면 404."""
    if profile_id not in _profiles:
        raise HTTPException(404, "profile not found")
    return _profiles[profile_id]


@career.post("/analyze")
async def analyze_posting(body: AnalyzeIn):
    """채용공고 원문 + 프로필 → 적합도 분석 JSON.

    응답의 reframe_evidence 필드가 CBT 상담과의 연결고리다:
    프론트는 이 문장들을 상담 화면에 "사실 근거 카드"로 보여줄 수 있고,
    /v1/respond 호출 시 text 에 이어붙여 재구성 답변의 근거로 쓸 수도 있다.
    """
    posting = body.posting.strip()
    if len(posting) < 30:
        raise HTTPException(400, "공고 원문이 너무 짧습니다. 공고 전문을 복사해 붙여넣어 주세요.")
    # 프롬프트/비용 폭주 방지: 공고가 지나치게 길면 앞부분만 사용 (핵심 요건은 보통 앞에 있음)
    posting = posting[:6000]

    profile = _resolve_profile(body.profile, body.profile_id)
    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM},
        {"role": "user", "content":
            f"[채용공고]\n{posting}\n\n[지원자 프로필]\n{_profile_text(profile)}"},
    ]
    options = dict(body.llm or {})
    options.setdefault("temperature", 0.2)   # 분석은 사실 위주 → 낮은 온도
    # JSON 이 토큰 상한에서 잘리지 않게 넉넉히 요청
    # (서버 .env 의 AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT 가 이보다 작으면 그 값으로 잘림)
    options.setdefault("max_completion_tokens", 2200)
    raw = await _chat_collect_or_502(messages, options)
    return _parse_json_or_raw(raw)


@career.post("/cover-letter")
async def cover_letter(body: CoverLetterIn):
    """공고 + 문항 + 저장된 경험 → 자소서 초안을 SSE 스트리밍으로 보낸다.

    이벤트 형식은 /v1/respond 와 동일(meta → token* → done)이라
    프론트의 기존 SSE 처리 코드를 그대로 재사용할 수 있다.
    """
    posting = body.posting.strip()[:6000]
    if len(posting) < 30:
        raise HTTPException(400, "공고 원문이 너무 짧습니다. 공고 전문을 복사해 붙여넣어 주세요.")
    profile = _resolve_profile(body.profile, body.profile_id)

    messages = [
        {"role": "system", "content": COVER_LETTER_SYSTEM},
        {"role": "user", "content":
            f"[채용공고]\n{posting}\n\n"
            f"[자소서 문항]\n{body.question}\n(글자수 제한: 공백 포함 {body.max_chars}자 이내)\n\n"
            f"[지원자의 실제 경험 — 이 목록에 있는 것만 사용]\n{_profile_text(profile)}"},
    ]
    options = dict(body.llm or {})
    options.setdefault("temperature", 0.5)   # 글쓰기라 약간의 다양성
    # 자소서는 상담 답변보다 길다 → 여유 있는 기본값 (서버측 *_LIMIT 상한은 llm.py 가 강제)
    options.setdefault("max_completion_tokens", 2200)

    return _sse_response("cover_letter", messages, options,
                         extra_meta={"question": body.question, "max_chars": body.max_chars})


@career.post("/review")
async def review_draft(body: ReviewIn):
    """사용자가 쓴 자소서를 첨삭한다 — 총평/강점/수정제안/수정본을 SSE 로 스트리밍.

    공고(posting)를 함께 주면 공고 키워드 반영 여부까지 점검하고,
    프로필을 주면 "초안에 있는데 경험 목록에 없는 주장"을 잡아낼 수 있다.
    """
    draft = body.draft.strip()
    if len(draft) < 50:
        raise HTTPException(400, "첨삭할 자소서 원문이 너무 짧습니다. 전문을 붙여넣어 주세요.")
    draft = draft[:8000]  # 비용 폭주 방지

    # 프로필은 선택 — 있으면 사실 검증 근거로 쓰고, 없으면 첨삭만 한다
    profile_block = ""
    if body.profile is not None or body.profile_id:
        try:
            profile_block = f"\n\n[지원자의 실제 경험 목록 — 초안이 여기 없는 사실을 주장하면 지적할 것]\n{_profile_text(_resolve_profile(body.profile, body.profile_id))}"
        except HTTPException:
            pass  # profile_id 가 만료됐어도 첨삭 자체는 진행

    posting_block = f"\n\n[채용공고]\n{body.posting.strip()[:6000]}" if body.posting else ""
    question_block = f"\n\n[자소서 문항]\n{body.question}" if body.question else ""

    system = REVIEW_RESUME_SYSTEM if body.doc_type == "resume" else REVIEW_SYSTEM
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content":
            f"[자소서 초안 — 첨삭 대상]\n{draft}"
            f"{question_block}\n(글자수 제한: 공백 포함 {body.max_chars}자 이내)"
            f"{posting_block}{profile_block}"},
    ]
    options = dict(body.llm or {})
    options.setdefault("temperature", 0.3)   # 첨삭은 일관성이 중요 → 낮은 온도
    options.setdefault("max_completion_tokens", 1800)  # 제안 중심이라 이 정도면 충분
    return _sse_response("review", messages, options,
                         extra_meta={"max_chars": body.max_chars, "doc_type": body.doc_type})


@career.post("/search")
async def search_postings(body: SearchIn):
    """워크넷(고용24) 공식 오픈 API 로 채용공고 검색 → 프론트가 목록을 보여주고,
    사용자가 고른 공고의 posting_text 를 analyze / analyze-batch 로 넘기면 된다.

    설정: .env 에 WORKNET_API_KEY=<인증키> (work24.go.kr 오픈API 무료 신청).
    기관이 API 주소를 바꾸면 WORKNET_API_URL 로 교체 가능.
    """
    import os
    import xml.etree.ElementTree as ET
    import httpx as _httpx

    api_key = os.getenv("WORKNET_API_KEY", "")
    if not api_key:
        raise HTTPException(
            503,
            "WORKNET_API_KEY 가 설정되지 않았습니다. work24.go.kr → 오픈API 에서 무료 인증키를 "
            "신청해 .env 에 넣어주세요. 그동안은 공고를 직접 복붙하는 /analyze 를 쓰면 됩니다.")

    url = os.getenv("WORKNET_API_URL", "https://openapi.work.go.kr/opi/opi/opia/wantedApi.do")
    params = {"authKey": api_key, "callTp": "L", "returnType": "XML",
              "startPage": "1", "display": str(max(1, min(body.count, 10))),
              "keyword": body.keyword}
    if body.region:
        params["region"] = body.region
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as exc:
        raise HTTPException(502, f"워크넷 API 호출 실패: {exc} — 인증키/주소(WORKNET_API_URL)를 확인하세요.")

    postings = []
    for w in root.iter("wanted"):
        get = lambda tag: (w.findtext(tag) or "").strip()
        item = {
            "title": get("title"), "company": get("company"),
            "region": get("region"), "career": get("career"),
            "education": get("minEdubg"), "salary": get("sal") or get("salTpNm"),
            "close_date": get("closeDt"), "url": get("wantedInfoUrl") or get("wantedMobileInfoUrl"),
        }
        # analyze 에 바로 넣을 수 있는 텍스트 블록도 만들어 준다 (프론트 편의)
        item["posting_text"] = (
            f"{item['company']} — {item['title']}\n"
            f"지역: {item['region']} / 경력: {item['career']} / 학력: {item['education']}\n"
            f"급여: {item['salary']} / 마감: {item['close_date']}\n상세: {item['url']}")
        postings.append(item)
    return {"count": len(postings), "keyword": body.keyword, "postings": postings,
            "note": "목록 API 는 요약 정보만 준다. 정밀 분석은 상세 공고 본문을 복붙해 /analyze 에 넣는 것이 가장 정확하다."}


@career.post("/analyze-batch")
async def analyze_batch(body: AnalyzeBatchIn):
    """공고 여러 개를 병렬 분석해 적합도(fit_score) 내림차순으로 돌려준다.

    프론트 흐름: search(또는 복붙 여러 개) → analyze-batch 로 순위표 →
    사용자가 공고 선택 → cover-letter / review / resume 로 맞춤 작성·수정.
    """
    import asyncio

    if not (2 <= len(body.postings) <= 10):
        raise HTTPException(400, "postings 는 2~10개로 보내주세요. 1개면 /analyze 를 쓰세요.")
    profile = _resolve_profile(body.profile, body.profile_id)
    options = dict(body.llm or {})
    options.setdefault("temperature", 0.2)
    options.setdefault("max_completion_tokens", 2200)

    sem = asyncio.Semaphore(3)  # LLM 동시 호출 3개 제한 (레이트리밋/비용 보호)

    async def one(idx: int, posting: str) -> dict:
        text = posting.strip()[:6000]
        if len(text) < 30:
            return {"index": idx, "error": "공고가 너무 짧음"}
        messages = [
            {"role": "system", "content": ANALYZE_SYSTEM},
            {"role": "user", "content": f"[채용공고]\n{text}\n\n[지원자 프로필]\n{_profile_text(profile)}"},
        ]
        async with sem:
            try:
                result = _parse_json_or_raw(await _chat_collect(messages, options))
            except Exception as exc:
                return {"index": idx, "error": f"LLM 호출 실패: {exc}"}
        result["index"] = idx
        result["posting_preview"] = text[:80]
        return result

    results = await asyncio.gather(*(one(i, p) for i, p in enumerate(body.postings)))
    ranked = sorted(results, key=lambda r: r.get("fit_score") or -1, reverse=True)
    return {"count": len(ranked), "ranked": ranked,
            "caveat": "적합도는 참고 정보이며 합격 여부를 예측하지 않습니다."}


@career.post("/resume")
async def build_resume(body: ResumeIn):
    """프로필 → (공고 맞춤) 이력서 구조 초안 JSON.

    프론트는 이 JSON 을 이력서 템플릿에 그대로 흘려넣으면 된다.
    missing_info 필드가 비어있지 않으면 "이 정보를 채워주세요" UI 를 띄울 것.
    """
    profile = _resolve_profile(body.profile, body.profile_id)
    posting_block = f"\n\n[채용공고 — 이 직무에 맞게 강조점 조정]\n{body.posting.strip()[:6000]}" if body.posting else ""

    messages = [
        {"role": "system", "content": RESUME_SYSTEM},
        {"role": "user", "content": f"[지원자 프로필]\n{_profile_text(profile)}{posting_block}"},
    ]
    options = dict(body.llm or {})
    options.setdefault("temperature", 0.2)
    options.setdefault("max_completion_tokens", 1600)
    raw = await _chat_collect_or_502(messages, options)
    return _parse_json_or_raw(raw)


# ── SSE 응답 공통 도우미 (cover-letter 와 review 가 같이 쓴다) ──────────────

def _sse_response(feature: str, messages: list[dict], options: dict,
                  extra_meta: dict | None = None) -> StreamingResponse:
    """LLM 스트리밍을 /v1/respond 와 같은 이벤트 형식(meta→token*→done)으로 감싼다."""
    stream_id = "career-" + uuid.uuid4().hex[:8]

    async def stream() -> AsyncIterator[str]:
        yield sse({"type": "meta", "session_id": stream_id, "feature": feature,
                   **(extra_meta or {})})
        try:
            async for tok in services.llm.chat_stream_async(messages, options):
                yield sse(token_event(stream_id, tok))
        except Exception as exc:  # LLM 장애 시에도 SSE 로 에러를 알리고 정상 종료
            yield sse({"type": "error", "session_id": stream_id, "message": str(exc)})
        yield sse(done_event(stream_id))

    return StreamingResponse(stream(), media_type="text/event-stream")
