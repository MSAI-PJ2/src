"""Safety gate helpers for crisis routing."""

import httpx

from . import settings


_RISK_KEYWORDS = (
    "자살", "죽고싶", "자해", "끝내고싶", "사라지고싶",
    "살이유가없", "살이유없", "목숨", "뛰어내리", "죽어버",
)


def keyword_check(text: str) -> dict:
    """Offline/fallback keyword check for obvious crisis signals."""
    flat = text.replace(" ", "")
    matched = [keyword for keyword in _RISK_KEYWORDS if keyword in flat]
    if matched:
        return {"safe": False, "reason": "self_harm_signal", "matched": matched}
    return {"safe": True, "reason": None}


async def safety_check(text: str) -> dict:
    """Run Azure Content Safety when configured, otherwise use keyword fallback."""
    if settings.CONTENT_SAFETY_ENABLED and settings.CONTENT_SAFETY_ENDPOINT and settings.CONTENT_SAFETY_KEY:
        url = settings.CONTENT_SAFETY_ENDPOINT.rstrip("/") + "/contentsafety/text:analyze?api-version=2024-09-01"
        try:
            async with httpx.AsyncClient(timeout=settings.CONTENT_SAFETY_TIMEOUT) as client:
                resp = await client.post(
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
                return {
                    "safe": False,
                    "reason": reason,
                    "categories": categories,
                    "source": "content_safety",
                }
            return {
                "safe": True,
                "reason": None,
                "categories": categories,
                "source": "content_safety",
            }
        except Exception as exc:
            result = keyword_check(text)
            result["source"] = "keyword_fallback"
            result["cs_error"] = str(exc)[:140]
            return result

    result = keyword_check(text)
    result["source"] = "keyword"
    return result
