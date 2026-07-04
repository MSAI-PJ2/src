import os

MODEL_PATH = os.getenv("MODEL_PATH", "/models/cogdist")
MODEL_ID = os.getenv("MODEL_ID", "klue/roberta-large")
MODEL_VERSION = os.getenv("MODEL_VERSION", "multi_large_v2")
CLASSIFY_MODE = os.getenv("CLASSIFY_MODE", "multi_label")  # auto | single_label | multi_label
DEFAULT_THRESHOLD = float(os.getenv("DEFAULT_THRESHOLD", "0.55"))
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "32"))
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "128"))  # 학습·평가·threshold 튜닝이 전부 128 고정 — 서빙도 정렬 (129~160 구간은 미보정 영역, 2026-07-04 검수)
