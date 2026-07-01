# Gateway Container

This folder contains the Azure Container Apps `api-gateway` container source.
The repository root may contain other team code, so the gateway Docker build context is limited to `gateway-container/`.

## Layout

```text
gateway-container/
|-- .dockerignore
|-- .env.example
|-- API_CONTRACT.md
|-- README.md
|-- README_GATEWAY_SHARE.md
|-- docker-compose.yml
|-- scripts/
`-- services/
    |-- api-gateway/
    |   |-- Dockerfile
    |   |-- requirements.txt
    |   `-- app/
    |-- common/
    `-- retrieve/
```

## Stack

```text
Framework: FastAPI + Uvicorn
Container: Azure Container Apps / Azure Container Registry
Auth: temporary x-api-key
Classifier: internal cogdistmodel Container App
Safety: Azure AI Content Safety + keyword fallback
RAG: Azure AI Search
LLM: Azure OpenAI gpt-4.1-mini
Speech: Azure Speech STT/TTS
Session store: memory or Azure Cosmos DB NoSQL
```

## Important files

```text
services/api-gateway/app/main.py                         FastAPI route entrypoint
services/api-gateway/app/dag.py                          respond orchestration
services/api-gateway/app/adapters.py                     external service adapter boundary
services/api-gateway/app/request_context.py              /v1/respond request normalization
services/api-gateway/app/repositories/session_repository.py  memory/Cosmos session repository
services/api-gateway/app/payloads.py                     SSE/API payload builders
services/api-gateway/app/turns.py                        session turn builders
services/api-gateway/app/prompts.py                      LLM message builder
services/api-gateway/app/events.py                       SSE serialization
services/api-gateway/app/safety.py                       Content Safety + keyword fallback
services/api-gateway/app/tts.py                          TTS event payload builder
services/api-gateway/app/ranking.py                      RAG candidate rerank
services/common/llm_client.py                            Azure OpenAI/local LLM client
services/common/retrieve_client.py                       Azure AI Search low-level client
services/common/speech_client.py                         Azure Speech STT/TTS client
services/retrieve/client.py                              Retriever provider wrapper
scripts/                                                 Azure regression test scripts
API_CONTRACT.md                                          Frontend/test API contract
```

## ACR build

Run from repository root:

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f services/api-gateway/Dockerfile \
  gateway-container
```

Important: build context is `gateway-container`, not `.`. This keeps other repository code out of the gateway image.

## Local run

```bash
docker compose -f gateway-container/docker-compose.yml up --build api-gateway
```

## Security notes

- Do not commit real keys or secrets.
- Share only `gateway-container/.env.example`.
- Azure Container Apps secrets are managed through SecretRef.
- Do not hard-code the gateway API key in frontend code.

## Current validation status

- `/healthz` PASS
- Missing `x-api-key` returns 401 PASS
- `/v1/classify` PASS
- `/v1/respond` text/transcript/audio STT/TTS PASS
- crisis branch PASS
- Cosmos session persistence PASS

Document Intelligence OCR is a separate branch task.
Expected future input is `/v1/respond` with `input_type=document`; after OCR succeeds, it should feed the existing text DAG.
