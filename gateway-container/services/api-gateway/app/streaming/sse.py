"""SSE(Server-Sent Events) 직렬화."""
import json


def sse(obj: dict) -> str:
    """payload dict 하나를 SSE data 프레임 한 개로 직렬화한다."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
