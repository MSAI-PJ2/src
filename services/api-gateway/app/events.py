"""SSE serialization helpers for the API gateway."""
import json


def sse(obj: dict) -> str:
    """Serialize a payload as one Server-Sent Events data frame."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
