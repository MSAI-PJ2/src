import os


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1")


def _origins_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


KLUE_API_URL = os.getenv("KLUE_API_URL", "http://cogdist:8000")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
API_KEY = os.getenv("API_KEY", "")
API_KEY_REQUIRED = _bool_env("API_KEY_REQUIRED", False)
ALLOWED_ORIGINS = _origins_env(
    "ALLOWED_ORIGINS",
    "http://127.0.0.1:5173,http://localhost:5173",
)
CONTENT_SAFETY_ENABLED = _bool_env("CONTENT_SAFETY_ENABLED", False)
CONTENT_SAFETY_ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "")
CONTENT_SAFETY_KEY = os.getenv("CONTENT_SAFETY_KEY", "")
CONTENT_SAFETY_THRESHOLD = int(os.getenv("CONTENT_SAFETY_THRESHOLD", "2"))  # severity 0/2/4/6 이상 차단 기준
CONTENT_SAFETY_TIMEOUT = float(os.getenv("CONTENT_SAFETY_TIMEOUT", "5"))
RETRIEVE_PROVIDER = os.getenv("RETRIEVE_PROVIDER", "local")
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "4"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "20"))
SESSION_MAX_SESSIONS = int(os.getenv("SESSION_MAX_SESSIONS", "200"))
SESSION_CONTEXT_TURNS = int(os.getenv("SESSION_CONTEXT_TURNS", "6"))
SESSION_REPOSITORY = os.getenv("SESSION_REPOSITORY", "memory")


