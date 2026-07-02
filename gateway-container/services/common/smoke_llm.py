"""현재 LLM_PROVIDER 운영 경로(chat/chat_stream/chat_stream_async) 스모크 테스트.

실행:  LLM_PROVIDER=local python smoke_llm.py   (기본값 local = 무료 로컬 서버)
       LLM_PROVIDER=azure python smoke_llm.py   (.env에 Azure 설정 필요)

구조화(JSON) 출력 실험 경로 스모크는 llm_client_legacy.py 의 LegacyLLMClient 참고.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_client import LLMClient

MESSAGES = [
    {"role": "system", "content": "당신은 한국어 인지행동상담 보조자입니다. 두 문장 이내로 답하세요."},
    {"role": "user", "content": "사람들 앞에 서면 다 망칠 게 뻔해요."},
]


def main():
    client = LLMClient()
    print(f"provider: {client.provider}")
    print(f"model: {client.model}")

    started = time.time()
    print("\n[chat]")
    print(client.chat(MESSAGES, max_tokens=200))
    print(f"latency_sec: {time.time() - started:.3f}")

    print("\n[chat_stream]")
    for token in client.chat_stream(MESSAGES, max_tokens=200):
        print(token, end="", flush=True)
    print()

    print("\n[chat_stream_async]")

    async def run_async():
        async for token in client.chat_stream_async(MESSAGES, max_tokens=200):
            print(token, end="", flush=True)
        print()

    asyncio.run(run_async())


if __name__ == "__main__":
    main()
