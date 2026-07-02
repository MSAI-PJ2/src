"""환경변수 기반 게이트웨이 설정.

모든 런타임 설정은 이 파일 한 곳에서 읽는다. 새 설정을 추가할 때는
.env.example 에도 같이 기록해서 팀원이 로컬에서 재현할 수 있게 한다.
"""
import os


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1")


def _origins_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# --- 분류기 (내부 cogdist Container App) ---
KLUE_API_URL = os.getenv("KLUE_API_URL", "http://cogdist:8000")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
# strict: 정식 계약({primary, labels})만 허용. legacy: 과거 프로토타입 응답도 정규화(기본).
# 운영에서 cogdist 계약이 안정화되면 strict 로 올리는 것을 권장.
CLASSIFIER_RESPONSE_MODE = os.getenv("CLASSIFIER_RESPONSE_MODE", "legacy").strip().lower()

# --- 인증 / CORS ---
API_KEY = os.getenv("API_KEY", "")
API_KEY_REQUIRED = _bool_env("API_KEY_REQUIRED", False)
# api_key(현행) | entra(도입 예정, core/auth.py 가이드 참고)
AUTH_MODE = os.getenv("AUTH_MODE", "api_key").strip().lower()
ALLOWED_ORIGINS = _origins_env(
    "ALLOWED_ORIGINS",
    "http://127.0.0.1:5173,http://localhost:5173",
)

# --- Azure AI Content Safety ---
CONTENT_SAFETY_ENABLED = _bool_env("CONTENT_SAFETY_ENABLED", False)
CONTENT_SAFETY_ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "")
CONTENT_SAFETY_KEY = os.getenv("CONTENT_SAFETY_KEY", "")
CONTENT_SAFETY_THRESHOLD = int(os.getenv("CONTENT_SAFETY_THRESHOLD", "2"))  # severity 0/2/4/6, 이 값 이상이면 차단
CONTENT_SAFETY_TIMEOUT = float(os.getenv("CONTENT_SAFETY_TIMEOUT", "5"))

# --- RAG ---
RETRIEVE_PROVIDER = os.getenv("RETRIEVE_PROVIDER", "local")
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "4"))

# --- 세션 저장소 (memory | cosmos) ---
SESSION_REPOSITORY = os.getenv("SESSION_REPOSITORY", "memory")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "20"))
SESSION_MAX_SESSIONS = int(os.getenv("SESSION_MAX_SESSIONS", "200"))
SESSION_CONTEXT_TURNS = int(os.getenv("SESSION_CONTEXT_TURNS", "6"))
