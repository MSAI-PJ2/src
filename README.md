# API Gateway Latest Share

공유 목적: GitHub 연동용 최신 Gateway 코드 묶음입니다.

## 현재 반영 상태
- Framework: FastAPI + Uvicorn
- Container: Azure Container Apps / ACR image `gateway:azure-speech-tts-20260630`
- Auth: `x-api-key` 게이트웨이 API 키 필수
- Classifier: internal `cogdistmodel` 호출
- Safety: Azure AI Content Safety
- RAG: Azure AI Search `cbt-rag-search` / `cbt-rag-index`
- LLM: Azure OpenAI `gpt-4.1-mini` Chat Completions
- TTS: Azure Speech TTS 연결 완료, SSE `tts` event로 `audio_base64` 반환
- STT: payload contract 준비, 실제 provider 연결 대기

## 주요 경로
- `services/api-gateway/app/main.py`: FastAPI entrypoint
- `services/api-gateway/app/dag.py`: respond orchestration / SSE
- `services/api-gateway/app/schemas.py`: request schema
- `services/common/llm_client.py`: Azure OpenAI client
- `services/common/retrieve_client.py`: Azure AI Search client
- `services/common/speech_client.py`: Azure Speech TTS client

## 보안 주의
- 이 폴더에는 실제 secret/key를 넣지 않았습니다.
- 배포 시 secret은 Azure Container Apps secretref로 주입해야 합니다.
- 외부 호출은 `x-api-key` 헤더가 필요합니다.

## 최신 검증
- ACA revision: `api-gateway--0000013`
- TTS E2E: `status=completed`, `provider=azure`, `mime_type=audio/mpeg`, mp3 445K 생성 PASS
