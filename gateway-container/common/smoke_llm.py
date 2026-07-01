"""현재 LLM_PROVIDER로 LLMClient 구조화 출력을 검증하는 스모크 테스트.

실행:  LLM_PROVIDER=local python smoke_llm.py   (기본값 local = 무료 Nemotron)
       LLM_PROVIDER=azure python smoke_llm.py   (.env에 Azure 설정 필요)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_client import LLMClient

COUNSEL_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_distortion": {"type": "string"},
        "explanation": {"type": "string"},
        "reframe": {"type": "string"},
        "crisis": {"type": "boolean"},
    },
    "required": ["primary_distortion", "explanation", "reframe", "crisis"],
    "additionalProperties": False,
}

SYS = "당신은 인지왜곡 상담 보조입니다. 반드시 스키마에 맞는 JSON 하나만 출력하세요."


def run_one(client, utterance):
    messages = [
        {"role": "system", "content": SYS},
        {"role": "user", "content": utterance},
    ]
    started = time.time()
    result = client.chat_json(
        messages, COUNSEL_SCHEMA,
        schema_name="counsel_response", temperature=0.0, max_tokens=900,
    )
    latency = time.time() - started
    print(f"utterance: {utterance}")
    print(f"latency_sec: {latency:.3f}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()


def main():
    client = LLMClient()
    print(f"provider: {client.provider}")
    print(f"model: {client.model}")
    print()
    utterances = [
        "사람들 앞에 서면 다 망칠 게 뻔해요.",
        "다 끝내고 싶어요. 더 살 이유가 없는 것 같아요.",
    ]
    for utterance in utterances:
        run_one(client, utterance)


if __name__ == "__main__":
    main()
