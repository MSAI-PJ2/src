import os

MODEL_PATH = os.getenv("MODEL_PATH", "/models/cogdist")
MODEL_ID = os.getenv("MODEL_ID", "klue/roberta-large")
MODEL_VERSION = os.getenv("MODEL_VERSION", "multi_large_v2")
CLASSIFY_MODE = os.getenv("CLASSIFY_MODE", "multi_label")  # auto | single_label | multi_label
DEFAULT_THRESHOLD = float(os.getenv("DEFAULT_THRESHOLD", "0.55"))
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "32"))
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "160"))
