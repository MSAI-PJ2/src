"""[LEGACY] 프로토타입 LLM 경로 보관소 — 운영 코드에서 import 금지.

MVP 실험 단계에서 쓰던 경로들을 운영 클라이언트(llm_client.py)에서 분리해 보관한다:
    azure_responses / gpt5 / foundry   Azure OpenAI Responses API (GPT-5 계열 실험)
    azure_model_router                 Azure model router 배포 실험
    chat_json / first_json_block       구조화(JSON) 출력 실험

다시 필요해지면 여기서 꺼내 쓰되, 운영 채택 시 llm_client.py 로 승격하고
async 경로(chat_stream_async)를 함께 구현할 것.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from openai import AzureOpenAI

AZURE_ROUTER_ALIASES = {"azure_model_router", "model_router"}
AZURE_RESPONSES_ALIASES = {"azure_responses", "azure_openai_responses", "foundry", "gpt5"}


def first_json_block(s: str):
    """문자열에서 첫 번째 완결된 JSON object 블록을 찾아 반환한다."""
    start = None
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(s):
        if start is None:
            if ch == "{":
                start = i
                depth = 1
            continue
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


class LegacyLLMClient:
    """Responses API / model router 실험 경로. 운영 경로는 llm_client.LLMClient."""

    def __init__(self, provider: str | None = None):
        raw = (provider or os.getenv("LLM_PROVIDER", "")).strip().lower()

        if raw in AZURE_RESPONSES_ALIASES:
            self.provider = "azure_responses"
            self.endpoint = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
            self.api_key = os.getenv("AZURE_OPENAI_API_KEY") or ""
            self.model = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_MODEL") or ""
            missing = [
                name
                for name, value in {
                    "AZURE_OPENAI_ENDPOINT": self.endpoint,
                    "AZURE_OPENAI_API_KEY": self.api_key,
                    "AZURE_OPENAI_DEPLOYMENT": self.model,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError("Azure Responses provider missing env vars: " + ", ".join(missing))
            self.client = None
        elif raw in AZURE_ROUTER_ALIASES:
            self.provider = "azure_model_router"
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            api_key = os.getenv("AZURE_OPENAI_API_KEY")
            api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
            deployment = os.getenv("AZURE_OPENAI_ROUTER_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
            missing = [
                n
                for n, v in {
                    "AZURE_OPENAI_ENDPOINT": endpoint,
                    "AZURE_OPENAI_API_KEY": api_key,
                    "AZURE_OPENAI_ROUTER_DEPLOYMENT(or AZURE_OPENAI_DEPLOYMENT)": deployment,
                }.items()
                if not v
            ]
            if missing:
                raise ValueError("Azure provider missing env vars: " + ", ".join(missing))
            self.model = deployment
            self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        else:
            raise ValueError(
                f"LegacyLLMClient 는 프로토타입 경로 전용입니다 (got '{raw}'). "
                "운영 provider(local/azure)는 llm_client.LLMClient 를 사용하세요."
            )

    @staticmethod
    def _messages_to_responses_input(messages: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts.append(f"[{role}]\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_responses_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        pieces: list[str] = []
        for item in data.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text" and content.get("text"):
                    pieces.append(content["text"])
        return "".join(pieces)

    def _responses_create(self, messages, *, temperature=1.0, max_tokens: int | None = None) -> str:
        payload = {
            "model": self.model,
            "input": self._messages_to_responses_input(messages),
            "max_output_tokens": max_tokens or 900,
        }
        # GPT-5 계열 배포는 temperature=1 기본 — 명시된 경우에만 전달
        if temperature is not None:
            payload["temperature"] = temperature
        with httpx.Client(timeout=float(os.getenv("AZURE_OPENAI_TIMEOUT", "60"))) as client:
            resp = client.post(
                f"{self.endpoint}/openai/v1/responses",
                headers={"Content-Type": "application/json", "api-key": self.api_key},
                json=payload,
            )
            resp.raise_for_status()
            return self._extract_responses_text(resp.json())

    def chat(self, messages, *, temperature=0.0, max_tokens: int | None = None) -> str:
        if self.provider == "azure_responses":
            return self._responses_create(messages, temperature=1.0, max_tokens=max_tokens)
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature,
            max_completion_tokens=max_tokens or 900,
        )
        return resp.choices[0].message.content or ""

    def chat_json(self, messages, schema, *, schema_name="response", temperature=0.0, max_tokens=900) -> dict:
        """구조화(JSON) 출력 실험 경로: json_schema → json_object → free-form 순서로 시도."""
        if self.provider == "azure_responses":
            content = self.chat(messages, temperature=1.0, max_tokens=max_tokens)
            parsed = self._try_parse(content)
            if parsed is not None:
                return parsed
            return self._on_unparseable(content, schema, None)

        ladder = [
            {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
            {"type": "json_object"},
            None,
        ]
        last_error = None
        last_content = ""
        for rf in ladder:
            try:
                kwargs = {
                    "model": self.model, "messages": messages,
                    "temperature": temperature, "max_completion_tokens": max_tokens,
                }
                if rf is not None:
                    kwargs["response_format"] = rf
                resp = self.client.chat.completions.create(**kwargs)
                last_content = resp.choices[0].message.content or ""
                parsed = self._try_parse(last_content)
                if parsed is not None:
                    return parsed
                last_error = ValueError("response was not parseable as a JSON object")
            except Exception as exc:
                last_error = exc
        return self._on_unparseable(last_content, schema, last_error)

    @staticmethod
    def _try_parse(content):
        text = (content or "").strip()
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        block = first_json_block(text)
        if block:
            try:
                obj = json.loads(block)
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _on_unparseable(self, raw, schema, error=None):
        raise RuntimeError(f"LLM structured output parse failed (last_error={error}): {str(raw)[:300]}")
