"""retrieve 레인 계약(연결 구조). 백엔드 구현이 무엇이든 이 형태를 반환한다."""
from typing import Any, TypedDict


class RetrievedDoc(TypedDict):
    id: str
    content: str
    score: float
    metadata: dict[str, Any]   # 예: {"technique": "...", "distortions": ["흑백 사고", ...]}
