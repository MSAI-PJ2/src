"""[HTTP 경계] 프론트와 서버가 만나는 지점 — 요청 모양·인증·URL 목록이 전부 이 파일에 있다.

구획 목차 (Ctrl+F 로 "[구획" 검색):
    [구획 1] 요청 모델   프론트가 보내는 JSON 의 모양 (Pydantic — 형식이 틀리면 자동 422)
    [구획 2] 인증        x-api-key 검사 + Entra External ID 도입 가이드
    [구획 3] 라우트      URL 목록. HTTP 만 담당 — 상담 로직은 counsel/flow.py 에 위임

참고 — 함수 앞의 async: "비동기 함수". 한 요청이 Azure 응답을 기다리는 동안에도
서버가 다른 요청을 처리할 수 있게 해 준다. await = "결과가 올 때까지 이 요청만 대기".
"""
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import settings
from ..counsel import flow
from ..counsel.flow import RespondRequestContext
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
    """OCR 옵션. sender_names: 채팅방 상단에 뜨는 상대 이름 — 화자 판별 정확도를 높인다."""
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


# ══════════════════════════════════════════════════════════════════════════
# [구획 2] 인증 — "이 요청을 처리해도 되는가". 현행 = x-api-key 헤더 검사.
#
# 아래 [구획 3]의 모든 /v1 주소가 두 함수를 통과해야 실행된다 (Depends 의존성).
# 로그인(Entra) 도입 시에도 라우트는 그대로 두고 이 구획만 고치면 된다.
# ══════════════════════════════════════════════════════════════════════════

async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """임시 인증: 설정(API_KEY_REQUIRED=true)이 켜져 있으면 x-api-key 헤더를 검사.

    Header(default=None) = 요청 헤더에서 x-api-key 값을 꺼내 매개변수로 받는다는 뜻.
    키가 틀리면 401(인증 실패)을 던져서 요청 처리가 여기서 멈춘다.
    """
    if settings.API_KEY_REQUIRED and x_api_key != settings.API_KEY:
        raise HTTPException(401, "invalid api key")


async def current_user(authorization: str | None = Header(default=None)) -> str:
    """요청한 사용자가 누구인지(user_id)를 반환. 현재는 로그인이 없어 항상 "anonymous".

    ── [사람 작업 가이드] Microsoft Entra External ID(OIDC) 로그인 연동 ─────────
    흐름: 프론트가 Entra 로 로그인 → JWT(신원 증명 토큰) 발급 → 요청마다
          Authorization: Bearer <토큰> 첨부 → 이 함수가 토큰을 검증하고 user_id 추출.
    1. Entra 테넌트에 앱 등록 후 환경변수 준비:
         ENTRA_TENANT_ID   테넌트 GUID
         ENTRA_CLIENT_ID   이 API 앱 등록의 client id (토큰의 aud 와 일치해야 함)
         ENTRA_ISSUER      https://{테넌트GUID}.ciamlogin.com/{테넌트GUID}/v2.0
         ※ 서브도메인도 테넌트 '이름'이 아니라 GUID — 토큰의 iss 클레임과
           글자 단위로 같아야 검증을 통과한다.
    2. requirements.txt 에 PyJWT[crypto] 추가
    3. 이 함수에서:
       - authorization 헤더에서 "Bearer " 뒤의 토큰을 꺼낸다 (없으면 401)
       - 서명키(JWKS) 주소는 하드코딩하지 말고 OIDC 메타데이터에서 읽는다:
           GET {ENTRA_ISSUER}/.well-known/openid-configuration → 응답의 jwks_uri
         (issuer 뒤에 /discovery/v2.0/keys 를 그대로 붙이면 404 가 난다)
       - jwt.PyJWKClient(jwks_uri) 는 모듈 전역에 1회만 생성 (요청마다 생성 금지)
       - jwt.decode(token, key, algorithms=["RS256"],
                    audience=ENTRA_CLIENT_ID, issuer=ENTRA_ISSUER)
       - 검증 실패 → HTTPException(401) / 성공 → claims["oid"] 반환
    4. 환경변수 AUTH_MODE=entra 로 전환 — 라우트에 이미 의존성으로 걸려 있어서
       구현 전에는 아래 501 로 즉시 실패하고, 구현 후에는 자동으로 활성화된다.
       user_id 를 세션과 연결하려면 라우트에서 user_id: str = Depends(current_user) 로
       받아 흐름에 전달하고, 세션 저장/조회 조건에 포함시켜
       "내 세션만 접근"을 보장한다 (session.py 참고).
    5. require_api_key 는 서버 간 내부 호출용으로만 남기거나 제거.
    ─────────────────────────────────────────────────────────────────────
    """
    if settings.AUTH_MODE == "entra":
        # 구현 전에 실수로 entra 를 켜면 조용히 익명으로 동작하는 대신 명확히 실패시킨다
        raise HTTPException(501, "AUTH_MODE=entra is not implemented yet (see app/api/v1.py)")
    return "anonymous"


# ══════════════════════════════════════════════════════════════════════════
# [구획 3] 라우트 — 이 서버가 받는 모든 요청 주소(엔드포인트)
#
# 각 함수는 "요청을 받아서 → 알맞은 담당 모듈에 넘기는" 역할만 한다.
# 상담 로직은 counsel/flow.py, 외부 Azure 호출은 services/ 에 있다.
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
async def respond(body: RespondIn):
    """상담 응답 생성 — 이 서비스의 핵심 주소.

    입력 형태(이미지/음성/텍스트/빈 입력)에 따라 네 가지 흐름 중 하나로 보낸다.
    응답은 한 번에 주지 않고 SSE 스트리밍(생성되는 대로 조각조각 전송)으로 보낸다
    — 그래서 반환값이 일반 JSON 이 아니라 StreamingResponse 다.
    """
    context = RespondRequestContext.from_body(body)  # 요청을 내부 형태로 정리

    if context.requires_ocr:
        # 채팅 캡쳐 이미지만 왔음 → OCR(Document Intelligence) 후 상담 흐름으로
        stream = flow.ocr_then_respond_stream(
            context.session_id, context.input_meta, context.tts, context.llm)
    elif context.requires_stt:
        # 오디오만 왔음 → 먼저 음성→텍스트(STT) 변환 후 상담 흐름으로
        stream = flow.stt_then_respond_stream(
            context.session_id, context.input_meta, context.tts, context.llm)
    elif not context.has_text:
        # 텍스트도 오디오도 없음 → "입력을 보내달라"는 안내만 반환
        stream = flow.input_pending_stream(context.session_id, context.input_meta, context.tts)
    else:
        # 일반 텍스트 상담
        stream = flow.respond_stream(
            context.text or "", context.session_id, context.input_meta, context.tts, context.llm)

    return StreamingResponse(stream, media_type="text/event-stream")


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
