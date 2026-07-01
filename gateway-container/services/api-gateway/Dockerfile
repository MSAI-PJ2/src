FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# ffmpeg is required by pydub when converting webm/ogg/mp4 audio to wav for STT.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY services/api-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common /app/common
COPY services/retrieve /app/retrieve
COPY services/api-gateway/app /app/app

EXPOSE 8080

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
