"""Provider-switchable LLM client for local OpenAI-compatible servers and Azure OpenAI.

For GPT-5 family deployments on Azure AI Foundry, prefer LLM_PROVIDER=azure_responses,
which calls the Azure OpenAI Responses API endpoint (/openai/v1/responses).
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from openai import AzureOpenAI, OpenAI

LOCAL_PROVIDER_ALIASES = {"local", "nemotron"}
AZURE_PROVIDER_ALIASES = {"azure", "azure_openai"}
AZURE_ROUTER_ALIASES = {"azure_model_router", "model_router"}
AZURE_RESPONSES_ALIASES = {"azure_responses", "azure_openai_responses", "foundry", "gpt5"}


def first_json_block(s: str):
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


class LLMClient:
    def __init__(self, provider=None):
        raw = (provider or os.getenv("LLM_PROVIDER", "local")).strip().lower()

        if raw in LOCAL_PROVIDER_ALIASES:
            self.provider = "local"
            self.model = os.getenv("LOCAL_LLM_MODEL", "nemotron-3-super-no-think")
            self.client = OpenAI(
                base_url=os.getenv("LOCAL_LLM_BASE_URL", "http://192.168.1.155:8002/v1"),
                api_key=os.getenv("LOCAL_LLM_API_KEY", "dummy"),
            )
        elif raw in AZURE_RESPONSES_ALIASES:
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
        elif raw in AZURE_PROVIDER_ALIASES or raw in AZURE_ROUTER_ALIASES:
            is_router = raw in AZURE_ROUTER_ALIASES
            self.provider = "azure_model_router" if is_router else "azure"
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            api_key = os.getenv("AZURE_OPENAI_API_KEY")
            api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
            if is_router:
                deployment = os.getenv("AZURE_OPENAI_ROUTER_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
                dep_key = "AZURE_OPENAI_ROUTER_DEPLOYMENT(or AZURE_OPENAI_DEPLOYMENT)"
            else:
                deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
                dep_key = "AZURE_OPENAI_DEPLOYMENT"
            missing = [
                n
                for n, v in {
                    "AZURE_OPENAI_ENDPOINT": endpoint,
                    "AZURE_OPENAI_API_KEY": api_key,
                    dep_key: deployment,
                }.items()
                if not v
            ]
            if missing:
                raise ValueError("Azure provider missing env vars: " + ", ".join(missing))
            self.model = deployment
            self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        else:
            raise ValueError(
                f"Unsupported LLM_PROVIDER '{raw}' "
                "(use: local, nemotron, azure, azure_openai, azure_responses, azure_model_router)"
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

    @staticmethod
    def _int_env(name: str) -> int | None:
        raw = os.getenv(name)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _positive_int(value: int | None, fallback: int) -> int:
        if value is None:
            return fallback
        try:
            value = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(1, value)

    def _completion_token_limit(self, requested: int | None, fallback: int) -> int:
        """Resolve per-request token cap with server-side safety bounds.

        AZURE_OPENAI_MAX_COMPLETION_TOKENS is the default. The same value is
        also the default upper bound unless AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT
        is set. Request-level values can lower the cap or raise it only up to
        the server-side bound.
        """
        env_default = self._int_env("AZURE_OPENAI_MAX_COMPLETION_TOKENS") or self._int_env("LLM_MAX_COMPLETION_TOKENS")
        default = self._positive_int(env_default, fallback)
        desired = self._positive_int(requested, default) if requested is not None else default
        upper = self._int_env("AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT") or env_default or fallback
        return min(desired, self._positive_int(upper, default))

    def _responses_create(self, messages, *, temperature=1.0, max_tokens: int | None = None) -> str:
        token_limit = self._completion_token_limit(max_tokens, 900)
        payload = {
            "model": self.model,
            "input": self._messages_to_responses_input(messages),
            "max_output_tokens": token_limit,
        }
        # GPT-5 deployments often default to temperature=1; omit temperature unless explicitly non-None.
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

    def _azure_chat_kwargs(self, messages, *, temperature=0.0, max_tokens: int | None = None, stream=False) -> dict:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": float(os.getenv("AZURE_OPENAI_TOP_P", "1.0")),
            "frequency_penalty": float(os.getenv("AZURE_OPENAI_FREQUENCY_PENALTY", "0.0")),
            "presence_penalty": float(os.getenv("AZURE_OPENAI_PRESENCE_PENALTY", "0.0")),
            "max_completion_tokens": self._completion_token_limit(max_tokens, 900),
        }
        if stream:
            kwargs["stream"] = True
        return kwargs

    def chat(self, messages, *, temperature=0.0, max_tokens: int | None = None) -> str:
        token_limit = self._completion_token_limit(max_tokens, 900)
        if self.provider == "azure_responses":
            return self._responses_create(messages, temperature=1.0, max_tokens=token_limit)
        if self.provider in {"azure", "azure_model_router"}:
            resp = self.client.chat.completions.create(
                **self._azure_chat_kwargs(messages, temperature=temperature, max_tokens=token_limit)
            )
        else:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=temperature, max_tokens=token_limit,
            )
        return resp.choices[0].message.content or ""

    def chat_stream(self, messages, *, temperature=0.0, max_tokens: int | None = None):
        token_limit = self._completion_token_limit(max_tokens, 900)
        if self.provider == "azure_responses":
            # Non-streaming smoke path: yield once so existing SSE code still works.
            text = self._responses_create(messages, temperature=1.0, max_tokens=token_limit)
            if text:
                yield text
            return
        if self.provider in {"azure", "azure_model_router"}:
            stream = self.client.chat.completions.create(
                **self._azure_chat_kwargs(messages, temperature=temperature, max_tokens=token_limit, stream=True)
            )
        else:
            stream = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=temperature, max_tokens=token_limit, stream=True,
            )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content

    def chat_json(self, messages, schema, *, schema_name="response", temperature=0.0, max_tokens=900) -> dict:
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
                if self.provider in {"azure", "azure_model_router"}:
                    kwargs = self._azure_chat_kwargs(messages, temperature=temperature, max_tokens=max_tokens)
                else:
                    kwargs = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
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
