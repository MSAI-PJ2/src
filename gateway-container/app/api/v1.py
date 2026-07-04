"""[HTTP 경계] 프론트와 서버가 만나는 지점 — 요청 모양·인증·URL 목록이 전부 이 파일에 있다.

구획 목차 (Ctrl+F 로 "[구획" 검색):
    [구획 1] 요청 모델   프론트가 보내는 JSON 의 모양 (Pydantic — 형식이 틀리면 자동 422)
    [구획 2] 인증        x-api-key 검사 + Entra External ID 도입 가이드
    [구획 3] 라우트      URL 목록. HTTP 만 담당 — 상담 로직은 respond/flow.py 에 위임

참고 — 함수 앞의 async: "비동기 함수". 한 요청이 Azure 응답을 기다리는 동안에도
서버가 다른 요청을 처리할 수 있게 해 준다. await = "결과가 올 때까지 이 요청만 대기".
"""
import asyncio
import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import settings
from ..profile import profile_repository
from ..respond import flow
from ..respond.flow import RespondRequestContext
from ..services import services
from ..session import session_repository


# ══════════════════════════════════════════════════════════════════════════
# [구획 1] 요청 모델 — 프론트엔드가 보내는 JSON 의 모양(스키마)
#
# Pydantic 모델 = "이 요청에는 이런 이름/타입의 필드가 온다"는 선언. FastAPI 가
# 요청 JSON 을 자동 검사해서 형식이 틀리면 코드 실행 전에 422 오류를 돌려준다.
# `str | None = None` 은 "문자열 또는 생략 가능(기본 None)".
# 필드의 자세한 의미는 API_CONTRACT.md 가 기준 문서다.
# ══════════════════════════════════════════════════════════════════════════

class ClassifyIn(BaseModel):
    """POST /v1/classify 의 입력."""
    text: str
    threshold: float | None = None  # 분류 확신 기준값 (생략 시 모델 기본값)


class BatchClassifyIn(BaseModel):
    """POST /v1/batch-classify 의 입력 — 문장 여러 개."""
    texts: list[str]
    threshold: float | None = None


class AudioIn(BaseModel):
    """음성 입력. kind 가 base64 면 data 에, url 이면 url 에 오디오가 담긴다."""
    kind: Literal["url", "base64"] = "url"  # base64 는 소용량 테스트용
    url: str | None = None
    data: str | None = None                 # base64 로 인코딩된 오디오 바이트
    mime_type: str | None = None            # 예: audio/webm, audio/wav
    language: str | None = "ko-KR"


class ImageIn(BaseModel):
    """채팅 캡쳐 이미지 입력 (jpeg/png). base64 또는 url 로 전달."""
    kind: Literal["url", "base64"] = "base64"
    url: str | None = None
    data: str | None = None      # base64 로 인코딩된 이미지 바이트
    mime_type: str | None = None  # 예: image/jpeg, image/png


class OcrIn(BaseModel):
    """OCR 옵션 — 이미지를 어떻게 해석할지.

    profile: generic(일반 이미지 — 텍스트 전체 추출, 기본)
           | kakao(카톡 캡쳐 — 화자 분리 후 "나" 발화만 상담 입력으로)
    sender_names: kakao 프로파일에서 채팅방 상단 상대 이름 — 화자 판별 정확도를 높인다.
    """
    profile: Literal["generic", "kakao"] = "generic"
    sender_names: list[str] | None = None


class SttIn(BaseModel):
    """음성→텍스트 관련 정보. transcript(전사문)가 이미 있으면 STT 를 건너뛴다."""
    provider: str | None = None
    language: str | None = "ko-KR"
    transcript: str | None = None
    confidence: float | None = None


class TtsIn(BaseModel):
    """텍스트→음성 옵션. enabled=true 면 답변을 음성으로도 합성해 준다."""
    enabled: bool = False
    provider: str | None = None
    voice: str | None = None      # 예: ko-KR-SunHiNeural
    format: str | None = "mp3"
    speed: float | None = None


class LlmIn(BaseModel):
    """요청 단위 LLM 옵션. 서버측 상한(AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT)이 항상 우선."""
    max_completion_tokens: int | None = None  # 답변 최대 길이(토큰)
    temperature: float | None = None          # 높을수록 답변이 다양/무작위


class RespondIn(BaseModel):
    """POST /v1/respond 의 입력 — 텍스트/음성/전사문/채팅캡쳐 이미지 중 하나로 상담을 요청한다."""
    text: str | None = None
    session_id: str | None = None   # 대화방 ID. 같은 ID 로 보내면 대화가 이어진다
    input_type: Literal["text", "audio", "transcript", "image"] | None = None
    audio: AudioIn | None = None
    image: ImageIn | None = None
    ocr: OcrIn | None = None
    stt: SttIn | None = None
    tts: TtsIn | None = None
    llm: LlmIn | None = None
    client: dict[str, Any] | None = None    # 프론트가 넣는 자유 필드 (그대로 저장됨)
    metadata: dict[str, Any] | None = None

    def effective_text(self) -> str | None:
        """실제 처리할 텍스트를 고른다: text 가 있으면 text, 없으면 stt.transcript."""
        text = (self.text or "").strip()
        if text:
            return text
        transcript = ((self.stt.transcript if self.stt else None) or "").strip()
        return transcript or None

    def input_meta(self) -> dict[str, Any]:
        """어떤 형태의 입력이었는지 기록용으로 정리 (세션 저장·meta 이벤트에 들어감)."""
        default_type = "image" if self.image else ("audio" if self.audio else "text")
        return {
            "input_type": self.input_type or default_type,
            "audio": self.audio.model_dump(exclude_none=True) if self.audio else None,
            "image": self.image.model_dump(exclude_none=True) if self.image else None,
            "ocr": self.ocr.model_dump(exclude_none=True) if self.ocr else None,
            "stt": self.stt.model_dump(exclude_none=True) if self.stt else None,
            "client": self.client,
            "metadata": self.metadata,
        }


class SessionCreateIn(BaseModel):
    """POST /v1/sessions 의 입력. session_id 생략 시 서버가 발급."""
    session_id: str | None = None


class SurveyIn(BaseModel):
    """POST /v1/profile/survey 의 입력 — 프론트 설문 페이지의 payload 와 1:1.

    세부 항목은 아직 프론트가 다듬는 중이라 dict 로 느슨하게 받는다 (필드가 늘어도
    서버 수정 없이 그대로 저장). 서버가 실제로 "해석"하는 값은 location.sido/sigungu
    하나뿐이며(위기 지역 안내용 — profile.py 의 한글 미러 참고) 나머지는 보관만 한다.
    """
    nickname: str | None = None
    location: dict[str, Any] | None = None           # {"sido": 시도, "sigungu": 시군구|null}
    emergency_contact: dict[str, Any] | None = None  # 비상 연락처 (동의 플래그 포함)
    survey: dict[str, Any] | None = None             # 사전 설문 문항 응답들
    privacy: dict[str, Any] | None = None            # 약관/민감정보/위치 동의 플래그들


# ══════════════════════════════════════════════════════════════════════════
# [구획 2] 인증 — "이 요청을 처리해도 되는가"
#
# 아래 [구획 3]의 모든 /v1 주소가 require_api_key + current_user 를 통과해야 실행된다.
# 두 가지 모드를 환경변수 AUTH_MODE 로 고른다:
#   api_key(기본)  x-api-key 헤더 검사, current_user 는 "anonymous"
#   entra          Microsoft Entra External ID 의 JWT(Bearer) 검증 → user_id 반환
# entra 코드는 이미 구현돼 있고 잠들어 있다 — 아래 4개 환경변수만 채우고
# AUTH_MODE=entra 로 바꾸면 즉시 게이트로 작동한다 (.env.example 참고).
# ══════════════════════════════════════════════════════════════════════════

async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """임시 인증: 설정(API_KEY_REQUIRED=true)이 켜져 있으면 x-api-key 헤더를 검사.

    Header(default=None) = 요청 헤더에서 x-api-key 값을 꺼내 매개변수로 받는다는 뜻.
    키가 틀리면 401(인증 실패)을 던져서 요청 처리가 여기서 멈춘다.
    entra 모드로 넘어가면 require_api_key 는 서버-서버 내부 호출용으로만 남기거나 제거한다.
    """
    if settings.API_KEY_REQUIRED and x_api_key != settings.API_KEY:
        raise HTTPException(401, "invalid api key")


class EntraTokenVerifier:
    """Microsoft Entra External ID(OIDC) JWT 검증기.

    JWKS(서명키) 주소는 하드코딩하지 않고 OIDC 메타데이터에서 읽는다
    (issuer 뒤에 /discovery/v2.0/keys 를 그대로 붙이면 404 가 나는 함정 회피).
    PyJWKClient 가 서명키를 캐시하므로 매 요청 네트워크 호출이 없다.
    무거운 의존성(PyJWT)은 이 클래스가 실제로 만들어질 때만 import 한다
    → api_key 모드에서는 PyJWT 가 없어도 서버가 뜬다.
    """

    def __init__(self) -> None:
        import jwt  # PyJWT[crypto] — entra 모드에서만 필요

        self._jwt = jwt
        tenant = settings.ENTRA_TENANT_ID
        self.audience = settings.ENTRA_CLIENT_ID
        # issuer 를 안 주면 테넌트 GUID 로 CIAM 기본 형태를 만든다 (필요 시 명시 override)
        self.issuer = settings.ENTRA_ISSUER or (
            f"https://{tenant}.ciamlogin.com/{tenant}/v2.0" if tenant else "")
        missing = [n for n, v in {"ENTRA_CLIENT_ID": self.audience,
                                  "ENTRA_ISSUER(or ENTRA_TENANT_ID)": self.issuer}.items() if not v]
        if missing:
            raise ValueError("entra auth missing env vars: " + ", ".join(missing))

        # OIDC 메타데이터에서 jwks_uri 를 읽어 서명키 클라이언트를 1회 생성
        meta_url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
        jwks_uri = httpx.get(meta_url, timeout=10).raise_for_status().json()["jwks_uri"]
        self._jwks = jwt.PyJWKClient(jwks_uri)

    def verify(self, token: str) -> str:
        """토큰 서명·issuer·audience·만료를 검증하고 user_id(oid 우선, 없으면 sub)를 반환."""
        signing_key = self._jwks.get_signing_key_from_jwt(token).key
        claims = self._jwt.decode(token, signing_key, algorithms=["RS256"],
                                  audience=self.audience, issuer=self.issuer)
        return claims.get("oid") or claims.get("sub") or "unknown"


_verifier: EntraTokenVerifier | None = None


def _get_verifier() -> EntraTokenVerifier:
    """검증기를 첫 entra 요청 때 1회 생성해 재사용 (테스트는 이 전역을 가짜로 교체)."""
    global _verifier
    if _verifier is None:
        _verifier = EntraTokenVerifier()
    return _verifier


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization.split(" ", 1)[1].strip()


# 가상 ID(x-user-id) 허용 형식: 영문/숫자/일부 기호, 최대 64자.
# UUID 가 대표 사용처지만 형식을 UUID 로 못박지는 않는다 (테스트용 짧은 ID 허용).
_VIRTUAL_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


async def current_user(authorization: str | None = Header(default=None),
                       x_user_id: str | None = Header(default=None)) -> str:
    """요청한 사용자의 user_id 를 반환.

    ── 가구현 로그인 (api_key 모드, 현행) ─────────────────────────────────
    정식 로그인 전까지는 프론트엔드가 "가상 ID"를 발급·보관하고 매 요청의
    x-user-id 헤더로 보낸다 (프론트: UUID 를 만들어 세션/저장소에 저장 후 재사용).
    - 헤더가 있으면 그 값이 곧 user_id — 프로필/설문/위기 지역 안내가 사용자별로 동작.
    - 헤더가 없으면 기존과 동일하게 "anonymous" (하위 호환 — 이전 프론트도 그대로 동작).
    - 형식이 틀리면 400 으로 명확히 거절한다 (조용히 anonymous 로 강등하면 프로필이
      엉뚱한 계정에 저장되는 사고를 늦게 발견하게 되므로).
    ⚠️ 가상 ID 는 "식별"이지 "인증"이 아니다 — 헤더를 아는 사람은 그 프로필을 읽을 수
    있다. 민감정보 보호가 필요해지는 시점에 아래 entra 모드로 전환한다.

    ── entra 모드 (정식 로그인, 구현 완료·잠들어 있음) ─────────────────────
    Authorization: Bearer <JWT> 를 검증해 user_id(oid) 를 돌려준다. 이때 x-user-id
    헤더는 무시된다 — 같은 자리(user_id)에 가상 ID 대신 실제 oid 가 꽂히므로,
    프로필·세션·지역 안내 코드는 한 줄도 바꾸지 않고 정식 로그인으로 전환된다.
    검증(서명·issuer·audience·만료)은 블로킹이라 to_thread 로 오프로딩한다.

    ── [남은 작업 — 세션을 user_id 로 스코프] ──────────────────────────────
    프로필(/v1/profile*)과 위기 지역 안내는 user_id 배선이 끝났다. 세션까지
    "내 세션만 접근"을 원하면: sessions 라우트에서 이 값을 받아 session.py 저장소가
    user_id 필드를 갖도록 확장한다 (데이터 모델 변경이라 기존 익명 세션과의 호환을
    정하고 진행).
    ─────────────────────────────────────────────────────────────────────
    """
    if settings.AUTH_MODE != "entra":
        virtual_id = (x_user_id or "").strip()
        if not virtual_id:
            return "anonymous"
        if not _VIRTUAL_ID_RE.match(virtual_id):
            raise HTTPException(400, "invalid x-user-id (allowed: A-Z a-z 0-9 _ . - , max 64)")
        return virtual_id

    token = _bearer_token(authorization)  # 없으면 401
    try:
        verifier = _get_verifier()        # 설정 누락이면 ValueError → 500(서버 오설정)
    except ValueError as exc:
        raise HTTPException(500, f"entra auth misconfigured: {exc}")
    try:
        return await asyncio.to_thread(verifier.verify, token)
    except Exception:                     # 서명 불일치·만료·aud/iss 불일치 등
        raise HTTPException(401, "invalid or expired token")


# ══════════════════════════════════════════════════════════════════════════
# [구획 3] 라우트 — 이 서버가 받는 모든 요청 주소(엔드포인트)
#
# 각 함수는 "요청을 받아서 → 알맞은 담당 모듈에 넘기는" 역할만 한다.
# 상담 로직은 respond/flow.py, 외부 Azure 호출은 services/ 에 있다.
# ══════════════════════════════════════════════════════════════════════════

router = APIRouter()
# /v1/* 주소는 전부 인증을 거친다. Depends(...) = "이 함수를 먼저 통과해야 함"
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key), Depends(current_user)])


@router.get("/healthz")
async def healthz():
    """서버 생존 확인용. Azure 가 주기적으로 호출해 서버가 살아있는지 본다."""
    return {"status": "ok"}


@v1.post("/classify")
async def classify(body: ClassifyIn):
    """문장 1개를 인지왜곡 분류기(cogdist)에 보내 라벨을 받는다."""
    return await services.classifier.classify_one(body.text, body.threshold)


@v1.post("/batch-classify")
async def batch_classify(body: BatchClassifyIn):
    """문장 여러 개를 한 번에 분류한다 (데이터 검증용)."""
    return await services.classifier.classify_batch(body.texts, body.threshold)


@v1.post("/respond")
async def respond(body: RespondIn, user_id: str = Depends(current_user)):
    """상담 응답 생성 — 이 서비스의 핵심 주소.

    입력 형태(이미지/음성/텍스트/빈 입력)에 따라 네 가지 흐름 중 하나로 보낸다.
    응답은 한 번에 주지 않고 SSE 스트리밍(생성되는 대로 조각조각 전송)으로 보낸다
    — 그래서 반환값이 일반 JSON 이 아니라 StreamingResponse 다.

    user_id: 위기 분기에서 프로필(설문에 저장된 지역)로 지역 핫라인을 찾을 때 쓴다.
    metadata.region 이 오면 그쪽이 우선 (respond/policy.py resolve_region 참고).
    """
    context = RespondRequestContext.from_body(body)  # 요청을 내부 형태로 정리

    if context.requires_ocr:
        # 채팅 캡쳐 이미지만 왔음 → OCR(Document Intelligence) 후 상담 흐름으로
        stream = flow.ocr_then_respond_stream(
            context.session_id, context.input_meta, context.tts, context.llm, user_id=user_id)
    elif context.requires_stt:
        # 오디오만 왔음 → 먼저 음성→텍스트(STT) 변환 후 상담 흐름으로
        stream = flow.stt_then_respond_stream(
            context.session_id, context.input_meta, context.tts, context.llm, user_id=user_id)
    elif not context.has_text:
        # 텍스트도 오디오도 없음 → "입력을 보내달라"는 안내만 반환 (위기 분기 없음 → user_id 불필요)
        stream = flow.input_pending_stream(context.session_id, context.input_meta, context.tts)
    else:
        # 일반 텍스트 상담
        stream = flow.respond_stream(
            context.text or "", context.session_id, context.input_meta, context.tts, context.llm,
            user_id=user_id)

    return StreamingResponse(stream, media_type="text/event-stream")


# ── 프로필 (가구현 로그인과 짝) — 저장 규칙은 app/profile.py, 가상 ID 는 [구획 2] 참고 ──

@v1.get("/profile")
async def get_profile(user_id: str = Depends(current_user)):
    """내(x-user-id) 프로필 조회. 없으면 404 — 프론트 로그인 페이지는 404 를 받으면
    POST /v1/profile 로 새로 만든다 (get_profile() or create_profile() 패턴)."""
    prof = await asyncio.to_thread(profile_repository.get, user_id)
    if prof is None:
        raise HTTPException(404, "profile not found")
    return prof


@v1.post("/profile")
async def create_profile(user_id: str = Depends(current_user)):
    """프로필 생성. 이미 있으면 그대로 반환 (여러 번 눌러도 안전 — 멱등)."""
    return await asyncio.to_thread(profile_repository.ensure, user_id)


@v1.post("/profile/survey")
async def submit_survey(body: SurveyIn, user_id: str = Depends(current_user)):
    """설문 저장 → survey_completed=True 로 완료 표시된 프로필을 돌려준다.

    location.sido/sigungu 는 한글 필드(시도/시군구)로 미러 저장되어, 이후 위기 발화 시
    metadata.region 없이도 프로필 지역으로 가까운 상담 창구를 안내한다.
    """
    payload = body.model_dump(exclude_none=True)
    return await asyncio.to_thread(profile_repository.save_survey, user_id, payload)


@v1.post("/sessions")
async def create_session(body: SessionCreateIn | None = None):
    """새 대화 세션(대화방)을 만든다. session_id 를 안 주면 서버가 발급."""
    return await session_repository.create(body.session_id if body else None)


@v1.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """세션의 저장된 대화 기록을 조회한다. 없으면 404."""
    state = await session_repository.snapshot(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return state


router.include_router(v1)
