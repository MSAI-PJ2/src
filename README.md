# API Gateway Latest Share

공유 목적: GitHub 연동용 최신 Gateway 코드 묶음입니다.

## 현재 반영 상태
- Framework: FastAPI + Uvicorn
- Container: Azure Container Apps / ACR
- Auth: `x-api-key` 게이트웨이 API 키 필수
- Classifier: internal `cogdistmodel` 호출
- Safety: Azure AI Content Safety
- RAG: Azure AI Search `cbt-rag-search` / `cbt-rag-index`
- LLM: Azure OpenAI `gpt-4.1-mini` Chat Completions
- Speech: Azure Speech SDK + pydub + ffmpeg 방식 유지
- TTS: SSE `tts` event에서 `audio.data`를 canonical로 사용하고 `audio_base64`는 호환 alias로 제공
- STT: `audio.kind=url|base64` 지원. 개선 브랜치에서는 `stt` SSE event로 processing/completed/error를 명시

## 주요 경로
- `services/api-gateway/app/main.py`: FastAPI entrypoint
- `services/api-gateway/app/dag.py`: respond orchestration / SSE / STT→DAG 연결
- `services/api-gateway/app/schemas.py`: request schema
- `services/common/llm_client.py`: Azure OpenAI client
- `services/common/retrieve_client.py`: Azure AI Search client
- `services/common/speech_client.py`: Azure Speech STT/TTS client
- `API_CONTRACT.md`: 프론트/테스트용 API 계약 문서

## 보안 주의
- 이 폴더에는 실제 secret/key를 넣지 않았습니다.
- 배포 시 secret은 Azure Container Apps secretref로 주입해야 합니다.
- 외부 호출은 `x-api-key` 헤더가 필요합니다.

## 최신 검증
- 로컬 개선 브랜치: `py_compile` 통과
- Azure 배포 검증 전까지 `API_CONTRACT.md`는 테스트 후보 계약으로 취급
- Azure 테스트 PASS 후 구조 리팩터링 진행 예정
