"""[진입점] 서버가 시작될 때 가장 먼저 실행되는 파일.

Dockerfile 의 `uvicorn app.main:app` 이 이 파일의 `app` 객체(FastAPI 서버)를 띄운다.
여기서는 서버를 만들고 공통 설정(CORS, 요청ID)과 URL 목록(api/v1.py)을 연결만 한다.
"무엇을 응답할지"의 실제 내용은 전부 다른 파일에 있다:
    URL 목록·인증·요청모양  → api/v1.py
    상담 흐름(기계장치)     → respond/flow.py
    정책·프롬프트(사람편집) → respond/policy.py
"""
from dotenv import load_dotenv
load_dotenv()


import logging
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import settings
from .api.v1 import router

# Azure 모니터링(App Insights) 연결 — 연결 문자열 env 가 있는 배포 환경에서만 켜진다.
# 로컬 PC 에는 env 가 없으므로 이 블록은 그냥 건너뛰고, 서버는 정상 기동한다.
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor()
    except Exception as exc:  # 모니터링이 실패해도 서버 기동을 막으면 안 된다
        logging.getLogger(__name__).warning("App Insights 초기화 실패(기동 계속): %s", exc)

# FastAPI 서버 객체 생성 — 이 변수 이름(app)을 uvicorn 이 찾는다
app = FastAPI(title="mlnode-api-gateway")

# CORS: 브라우저가 다른 도메인(프론트엔드 주소)에서 이 서버를 호출하도록 허용하는 설정.
# 허용 주소 목록은 settings.ALLOWED_ORIGINS (환경변수 ALLOWED_ORIGINS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 미들웨어: 모든 요청이 거쳐가는 공통 처리. 여기서는 응답 헤더에 요청 고유번호를
# 붙여서, 문제가 생겼을 때 로그에서 "어느 요청이었는지" 추적할 수 있게 한다.
@app.middleware("http")
async def request_id(request: Request, call_next):
    response = await call_next(request)  # 실제 처리(라우터)를 먼저 실행하고
    response.headers["X-Request-ID"] = str(uuid.uuid4())  # 응답에 고유번호를 붙인다
    return response


# api.py 에 정의된 URL 들(/healthz, /v1/...)을 서버에 등록
app.include_router(router)
