# Gateway Container Share Guide

This guide summarizes the files inside `gateway-container/` for code sharing and review.
No real keys or secrets are included.

## Included scope

```text
services/api-gateway/  FastAPI gateway application
services/common/       LLM, Azure Search, Speech shared clients
services/retrieve/     Retriever provider wrapper
scripts/               Gateway regression test scripts
API_CONTRACT.md        API/SSE contract for frontend and tests
.env.example           Safe environment variable template
```

## Build path

Run from repository root:

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f services/api-gateway/Dockerfile \
  gateway-container
```

`gateway-container/.dockerignore` uses default-deny rules. The gateway image includes only `services/api-gateway/`, `services/common/`, and `services/retrieve/`.

## Main APIs

See `API_CONTRACT.md` for details.

```text
GET  /healthz
POST /v1/classify
POST /v1/respond
GET  /v1/sessions/{session_id}
```

All APIs except `/healthz` require the `x-api-key` header in test/production deployment.

## SSE event summary

```text
meta            classification/session metadata
chunks          retrieved RAG chunks
token           streamed LLM text token
crisis          self-harm/safety barrier response
stt             speech-to-text status/result
tts             text-to-speech status/result
input_required  accepted input but missing transcript/text
done            stream completed
```

## Test scripts

Run from repository root:

```bash
python gateway-container/scripts/gateway_sse_text_test.py
python gateway-container/scripts/gateway_sse_transcript_test.py
python gateway-container/scripts/gateway_sse_tts_test.py
python gateway-container/scripts/gateway_sse_audio_stt_test.py
python gateway-container/scripts/gateway_cosmos_session_test.py
```

Required environment variables:

```bash
export GW_FQDN=<api-gateway-fqdn>
export API_KEY_VALUE=<gateway-api-key>
```
