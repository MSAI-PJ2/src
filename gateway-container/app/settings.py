"""[설정] 서버의 모든 조정값을 환경변수에서 읽는 곳.

환경변수 = 코드 밖(.env 파일이나 Azure 설정)에서 주입하는 값. 키/주소처럼
환경마다 다르거나 비밀인 값을 코드에 하드코딩하지 않기 위해 쓴다.
os.getenv("이름", "기본값") = 환경변수가 없으면 기본값 사용.
새 설정을 추가하면 .env.example 에도 같이 기록해 팀원이 알 수 있게 한다.
"""
import os


def _bool(name: str, default: bool = False) -> bool:
    """환경변수는 전부 문자열이라, "true"/"1" 을 파이썬 불리언으로 바꿔 준다."""
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in ("true", "1")


# --- 분류기: 인지왜곡 분류 모델이 떠 있는 내부 컨테이너 주소 ---
KLUE_API_URL = os.getenv("KLUE_API_URL", "http://cogdist:8000")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))

# --- 인증 / CORS ---
# API_KEY_REQUIRED=true 면 모든 /v1 요청에 x-api-key 헤더가 있어야 한다
API_KEY = os.getenv("API_KEY", "")
API_KEY_REQUIRED = _bool("API_KEY_REQUIRED", False)
# AUTH_MODE: api_key(현행) | entra(아래 ENTRA_* 3개만 채우면 즉시 켜짐 — api/v1.py 구획 2)
AUTH_MODE = os.getenv("AUTH_MODE", "api_key").strip().lower()
# Microsoft Entra External ID (AUTH_MODE=entra 일 때만 사용)
ENTRA_TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")   # 테넌트 GUID (ISSUER 생략 시 여기서 유도)
ENTRA_CLIENT_ID = os.getenv("ENTRA_CLIENT_ID", "")   # 이 API 앱 등록의 client id (토큰 aud)
ENTRA_ISSUER = os.getenv("ENTRA_ISSUER", "")         # 예: https://{테넌트GUID}.ciamlogin.com/{테넌트GUID}/v2.0
# 브라우저에서 이 서버를 호출할 수 있는 프론트엔드 주소 목록 (쉼표 구분)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",")
    if origin.strip()
]

# --- Azure Content Safety: 위험(자살/자해 등) 발화 탐지 서비스 ---
CONTENT_SAFETY_ENABLED = _bool("CONTENT_SAFETY_ENABLED", False)
CONTENT_SAFETY_ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "")
CONTENT_SAFETY_KEY = os.getenv("CONTENT_SAFETY_KEY", "")
# 위험 점수(severity)가 이 값 이상이면 차단. Azure 기준 0/2/4/6 단계 (0=안전, 2=mild, 4=medium, 6=high)
#   ⚠ 4 상향은 실측으로 기각됨 (2026-07-04, gateway_safety_sweep 실험): Azure 는
#   의도·계획 없는 자살사고 표현("죽고 싶다는 생각이 며칠째…")을 severity 2 로 매기므로,
#   4 로 올리면 자살사고가 차단 없이 일반 상담으로 흘러간다. 2 유지가 안전 하한선.
#   threshold 2 의 과차단(강한 자기비하 오차단) 문제는 임계값이 아니라 소프트 모드
#   (severity 2~3 = 상담 진행 + 핫라인 병기) 설계로 풀어야 한다 — 팀 논의 보류 중.
#   실측 도구: scripts/safety_threshold_probe.py
CONTENT_SAFETY_THRESHOLD = int(os.getenv("CONTENT_SAFETY_THRESHOLD", "2"))
CONTENT_SAFETY_TIMEOUT = float(os.getenv("CONTENT_SAFETY_TIMEOUT", "5"))

# --- RAG: 검색된 참고자료 중 프롬프트에 넣을 문서 개수 ---
# 구 이름(RERANK_TOP_N)으로 배포된 환경도 계속 동작하도록 둘 다 읽는다.
# (라벨 가산점 rerank 는 2026-07 제거 — respond/flow.py [구획 3] 주석 참고)
RAG_TOP_N = int(os.getenv("RAG_TOP_N", os.getenv("RERANK_TOP_N", "4")))

# --- 컨텍스트 정책: 저확신 강등 하한 (추가분) ---
# 왜곡 라벨인데 확신이 이 값 미만이면 CBT 프롬프트 대신 명확화(clarify)로 강등.
# 이 노브와 별개로, 분류기 응답의 threshold(배포 0.55)는 항상 왜곡 단정의 기본
# 하한으로 적용된다(policy.resolve — 멀티라벨 argmax 는 threshold 검사를 안 받으므로).
# 참고: 배포 multi_large_v2 실측 확신값은 0.8~0.99 대(낮게 깔리지 않음) — 이 값을
# 0.6~0.7 로 올리는 실험도 안전한 편. 0 = 추가 하한 없음(기본 threshold 만 적용).
POLICY_MIN_CONFIDENCE = float(os.getenv("POLICY_MIN_CONFIDENCE", "0.0"))

# --- 컨텍스트 병합: 선행 필터 (분류 "전에" 병합/단독을 결정 — 턴당 분류기 호출 항상 1회) ---
# 설계 변경(2026-07-04, 재현 지시): 이전 twopass(단독 분류 후 불충분이면 재분류)는 불충분
# 턴마다 분류기를 2회 호출해 CPU 서빙 병목(4vCPU 요구)이 됐다. 지금은 이미 로드된 세션에서
# 직전 라벨을 확인하는 "선행 필터"가 분류 입력(단독문/병합문)을 미리 고른다 — 추가 지연 0.
# 트리거(둘 중 하나, 단 novelty 게이트 통과 시에만 병합):
#   ① 직전 사용자 턴 라벨 = 불충분  (clarify 에 대한 재발화 — 수렴 케이스)
#   ② 현재 발화가 단문(SHORT_CHARS 이하) — 불충분의 길이 프록시. 파편이 "처음" 나온
#      턴(직전=확신 왜곡)을 ①이 못 잡는 구멍을 메운다 (실측: 회복된 파편 전원 16자 이하)
# ※ twopass 기준 실측(회복 0.04→0.56 · 오염/날조/오탐 0.00)은 참고치 — 선행 필터판의
#   성능 재검증은 로컬 API 테스트로 수행 예정.
CLASSIFY_PREMERGE = _bool("CLASSIFY_PREMERGE", True)          # false = 병합 자체를 끔
# 트리거 ② 의 단문 기준(자). 0 = ② 끔 (직전 라벨 필터만 사용)
CLASSIFY_PREMERGE_SHORT_CHARS = int(os.getenv("CLASSIFY_PREMERGE_SHORT_CHARS", "20"))
# 병합에 끌어올 직전 사용자 발화 수 — 3이 2보다 회복률 우위 (twopass 기준 0.56 vs 0.40). 0 = 병합 끔
CLASSIFY_CONTEXT_MAX_TURNS = int(os.getenv("CLASSIFY_CONTEXT_MAX_TURNS", "3"))
# 병합문 길이 상한(자) — 분류기 절단선(160토큰 ≈ 300자)의 안전 마진
CLASSIFY_CONTEXT_MAX_CHARS = int(os.getenv("CLASSIFY_CONTEXT_MAX_CHARS", "180"))

# --- 연속 '불충분' 완화 사다리 (respond/policy.py 구획 1·2) ---
# 병합 재분류를 거치고도 '불충분'이 이 횟수째 연속되면 질문을 멈추고 수용·동행
# 모드로 전환한다 (발화 회피 신호로 해석). 배포 가중치 실측: 문턱 4에서 회피형
# 12/12(100%) 도달 vs 비회피 0/108(0%) — 완전 분리. 문턱 2였다면 비회피의
# 10%(병합 켬)~45%(병합 끔)가 수용 모드로 오탈출했을 값.
# 0 = 사다리 전체 끔 (0=꺼짐 관례, POLICY_MIN_CONFIDENCE 와 동일). 최소 유효값은 2 권장.
INSUFFICIENT_ESCAPE_AFTER = int(os.getenv("INSUFFICIENT_ESCAPE_AFTER", "4"))

# --- 멀티라벨 보조 지침: 프롬프트에 넣을 왜곡 접근 지침의 최대 개수 (주 지침 포함) ---
# 멀티라벨 분류기는 왜곡을 여러 개 동시 선택할 수 있다(threshold 0.55 이상 전부).
# 기본 2 = 주 지침 + 보조 지침 1개. 지침을 3개 이상 쌓으면 서로 희석되어 답변이
# 산만해지므로 늘릴 때 주의. 1 = 보조 지침 끔 (primary 단독, 도입 이전 동작).
LABEL_GUIDANCE_MAX = int(os.getenv("LABEL_GUIDANCE_MAX", "2"))

# --- 위기 지역 연락처 DB (respond/policy.py 구획 3) ---
# HOTLINE_CONTAINER 를 채우면 켜짐 — 세션과 같은 Cosmos 계정(COSMOS_*)을 쓴다.
# 실제 배포 DB 예: 컨테이너 kfsp_centers (파티션키 /시도, 필드 기관명·전화·주소·시도·시군구).
HOTLINE_CONTAINER = os.getenv("HOTLINE_CONTAINER", "")
HOTLINE_DATABASE = os.getenv("HOTLINE_DATABASE", "")            # 비우면 COSMOS_DATABASE 사용
HOTLINE_TIMEOUT_SECONDS = float(os.getenv("HOTLINE_TIMEOUT_SECONDS", "3"))  # 초과 시 전국 공통만

# --- 사용자 프로필 DB (respond/policy.py 구획 3 의 region DB 조회 루트) ---
# USER_PROFILE_CONTAINER 를 채우면 region 을 프로필에서도 조회한다(우선순위는 metadata.region 이 위).
# 예: 컨테이너 user_profiles (파티션키 /user_id, 필드 시도·시군구). 비어 있으면 이 경로는 휴면.
USER_PROFILE_CONTAINER = os.getenv("USER_PROFILE_CONTAINER", "")
USER_PROFILE_DATABASE = os.getenv("USER_PROFILE_DATABASE", "")  # 비우면 COSMOS_DATABASE 사용
USER_PROFILE_TIMEOUT_SECONDS = float(os.getenv("USER_PROFILE_TIMEOUT_SECONDS", "3"))

# --- 세션(대화 기록) 저장소: memory(개발/테스트용, 서버 재시작 시 소멸) | cosmos(운영 DB) ---
SESSION_REPOSITORY = os.getenv("SESSION_REPOSITORY", "memory")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))      # 세션 유효시간(초)
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "20"))            # 세션당 최대 저장 턴 수
SESSION_CONTEXT_TURNS = int(os.getenv("SESSION_CONTEXT_TURNS", "6"))     # LLM 에 주는 최근 대화 수
