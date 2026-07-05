import os

MODEL_PATH = os.getenv("MODEL_PATH", "/models/cogdist")
MODEL_ID = os.getenv("MODEL_ID", "klue/roberta-large")
MODEL_VERSION = os.getenv("MODEL_VERSION", "multi_large_v2")
CLASSIFY_MODE = os.getenv("CLASSIFY_MODE", "multi_label")  # auto | single_label | multi_label
DEFAULT_THRESHOLD = float(os.getenv("DEFAULT_THRESHOLD", "0.55"))
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "32"))
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "160"))


# --- SHAP 설명(연산 과정 보기) ---
# 캐싱 없이 요청마다 새로 계산한다 (프론트 "연산 과정 보기" 버튼 클릭 시에만 호출됨).
# max_evals 가 클수록 정확하지만 느려진다 — SHAP 계산 기준 문장 하나에 수 초~수십 초.
SHAP_MAX_EVALS = int(os.getenv("SHAP_MAX_EVALS", "64"))
# --- SHAP 설명(연산 과정 보기) 전용 타임아웃 — /v1/predict 보다 훨씬 느릴 수 있어 별도로 둠 ---
SHAP_REQUEST_TIMEOUT_SECONDS = float(os.getenv("SHAP_REQUEST_TIMEOUT_SECONDS", "90"))
