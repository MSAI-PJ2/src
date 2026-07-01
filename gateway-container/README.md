# Gateway Container

이 폴더는 Azure Container Apps `api-gateway` 배포에 필요한 게이트웨이 컨테이너 작업물을 모아 둔 영역입니다.
리포지토리 루트에는 다른 팀 코드가 추가될 수 있으므로, 게이트웨이 빌드 컨텍스트는 이 폴더로 제한합니다.

## 구조

```text
gateway-container/
├── .dockerignore
├── .env.example
├── API_CONTRACT.md
├── README.md
├── README_GATEWAY_SHARE.md
├── docker-compose.yml
├── scripts/
├── api-gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
├── common/
└── retrieve/
```

## 주요 기술

```text
Framework: FastAPI + Uvicorn
Container: Azure Container Apps / Azure Container Registry
Auth: x-api-key 임시 인증
Classifier: internal cogdistmodel Container App
Safety: Azure AI Content Safety + keyword fallback
RAG: Azure AI Search
LLM: Azure OpenAI gpt-4.1-mini
Speech: Azure Speech STT/TTS
Session store: memory 또는 Azure Cosmos DB NoSQL
```

## 주요 파일

```text
api-gateway/app/main.py                         FastAPI route entrypoint
api-gateway/app/dag.py                          respond orchestration
api-gateway/app/adapters.py                     external service adapter boundary
api-gateway/app/request_context.py              /v1/respond request normalization
api-gateway/app/repositories/session_repository.py  memory/Cosmos session repository
api-gateway/app/payloads.py                     SSE/API payload builders
api-gateway/app/turns.py                        session turn builders
api-gateway/app/prompts.py                      LLM message builder
api-gateway/app/events.py                       SSE serialization
api-gateway/app/safety.py                       Content Safety + keyword fallback
api-gateway/app/tts.py                          TTS event payload builder
api-gateway/app/ranking.py                      RAG candidate rerank
common/llm_client.py                            Azure OpenAI/local LLM client
common/retrieve_client.py                       Azure AI Search low-level client
common/speech_client.py                         Azure Speech STT/TTS client
retrieve/client.py                              Retriever provider wrapper
scripts/                                        Azure regression test scripts
API_CONTRACT.md                                 Frontend/test API contract
```

## ACR 빌드

리포지토리 루트에서 실행할 때의 기준 명령입니다.

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f api-gateway/Dockerfile \
  gateway-container
```

중요: build context는 `.`가 아니라 `gateway-container`입니다. 그래야 리포지토리 루트의 다른 팀 코드가 게이트웨이 이미지 빌드에 포함되지 않습니다.

## 로컬 compose

```bash
docker compose -f gateway-container/docker-compose.yml up --build api-gateway
```

로컬에서 cogdist까지 함께 띄우려면 별도 cogdist 컨테이너 또는 `KLUE_API_URL` 설정이 필요합니다.

## 환경변수

- 실제 `.env`는 커밋하지 않습니다.
- 공유 가능한 템플릿은 `gateway-container/.env.example`입니다.
- Azure 배포 시 key류는 Azure Container Apps secretref로 주입합니다.

## 현재 검증 기준

```text
healthz PASS
auth 401 PASS
classify PASS
respond text PASS
crisis PASS
transcript PASS
TTS PASS
audio STT success/failure PASS
Cosmos session persistence PASS
```

## 다음 예정 작업

Document Intelligence OCR은 별도 브랜치에서 진행합니다.
예상 입력은 `/v1/respond`의 `input_type=document`이며, OCR 성공 후 기존 text DAG로 연결합니다.
