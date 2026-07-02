"""v1 라우터 묶음. 경로 구성은 main.py 가 아니라 여기서 본다."""
from fastapi import APIRouter

from . import classify, health, respond, sessions

router = APIRouter()
router.include_router(health.router)                      # /healthz (인증 없음)
router.include_router(classify.router, prefix="/v1")
router.include_router(respond.router, prefix="/v1")
router.include_router(sessions.router, prefix="/v1")
