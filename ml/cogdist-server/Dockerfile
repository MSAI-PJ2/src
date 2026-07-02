FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1 \
    MODEL_PATH=/models/cogdist \
    MODEL_ID=klue/roberta-large \
    MODEL_VERSION=multi_large_v2 \
    CLASSIFY_MODE=multi_label \
    DEFAULT_THRESHOLD=0.55 \
    MAX_LENGTH=160

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
