"""게이트웨이 진입점 — 앱 생성, 미들웨어, 라우터 등록만 한다.

엔드포인트 목록은 api/v1/, 상담 응답 흐름은 orchestrator/respond_flow.py 를 본다.
"""
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api.v1 import router as v1_router
from .core import settings, telemetry

telemetry.setup()  # App Insights — 연결 문자열이 있을 때만 활성화

app = FastAPI(title="mlnode-api-gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


app.include_router(v1_router)
