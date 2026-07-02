"""Azure Content Safety 기반 위험 발화 탐지 + 키워드 fallback.

반환 계약: {safe: bool, reason, source, ...}
source 필드로 어떤 경로가 판정했는지 항상 드러낸다:
    content_safety    Azure 판정 (정상 경로)
    keyword_fallback  Azure 호출 실패 → 키워드 검사로 대체 (cs_error 포함)
    keyword           Content Safety 비활성 상태의 키워드 검사
"""
import logging

import httpx

from ..core import settings

logger = logging.getLogger(__name__)

_RISK_KEYWORDS = (
    "자살", "죽고싶", "자해", "끝내고싶", "사라지고싶",
    "살이유가없", "살이유없", "목숨", "뛰어내리", "죽어버",
)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.CONTENT_SAFETY_TIMEOUT)
    return _client


def keyword_check(text: str) -> dict:
    """오프라인/fallback 키워드 검사 — 명백한 위기 신호만 잡는다."""
    flat = text.replace(" ", "")
    matched = [keyword for keyword in _RISK_KEYWORDS if keyword in flat]
    if matched:
        return {"safe": False, "reason": "self_harm_signal", "matched": matched}
    return {"safe": True, "reason": None}


async def safety_check(text: str) -> dict:
    """Content Safety 가 설정돼 있으면 사용, 아니면 키워드 검사."""
    if settings.CONTENT_SAFETY_ENABLED and settings.CONTENT_SAFETY_ENDPOINT and settings.CONTENT_SAFETY_KEY:
        url = settings.CONTENT_SAFETY_ENDPOINT.rstrip("/") + "/contentsafety/text:analyze?api-version=2024-09-01"
        try:
            resp = await _http().post(
                url,
                json={"text": text},
                headers={"Ocp-Apim-Subscription-Key": settings.CONTENT_SAFETY_KEY},
            )
            resp.raise_for_status()
            categories = {
                item["category"]: item["severity"]
                for item in resp.json().get("categoriesAnalysis", [])
            }
            flagged = {
                category: severity
                for category, severity in categories.items()
                if severity >= settings.CONTENT_SAFETY_THRESHOLD
            }
            if flagged:
                reason = "self_harm" if "SelfHarm" in flagged else max(flagged, key=flagged.get).lower()
                return {"safe": False, "reason": reason, "categories": categories, "source": "content_safety"}
            return {"safe": True, "reason": None, "categories": categories, "source": "content_safety"}
        except Exception as exc:
            # fallback 발생을 로그와 응답 source 에 명시한다 (조용한 대체 금지)
            logger.warning("Content Safety 호출 실패 — 키워드 fallback 사용: %s", exc)
            result = keyword_check(text)
            result["source"] = "keyword_fallback"
            result["cs_error"] = str(exc)[:140]
            return result

    result = keyword_check(text)
    result["source"] = "keyword"
    return result


class SafetyAdapter:
    async def check(self, text: str) -> dict:
        return await safety_check(text)
