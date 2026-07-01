#!/usr/bin/env python3
"""Collect /v1/respond SSE events for transcript input.

Required env:
- GW_FQDN
- API_KEY_VALUE
Optional env:
- TEST_SESSION_ID
- TEST_TRANSCRIPT
"""
import json
import os
import sys
import urllib.request
from pathlib import Path


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing env {name}", file=sys.stderr)
        sys.exit(2)
    return value


def main() -> int:
    gw = require_env("GW_FQDN")
    key = require_env("API_KEY_VALUE")
    payload = {
        "session_id": os.environ.get("TEST_SESSION_ID", "api-contract-transcript-1"),
        "input_type": "transcript",
        "stt": {
            "provider": os.environ.get("TEST_STT_PROVIDER", "mock"),
            "language": os.environ.get("TEST_LANGUAGE", "ko-KR"),
            "transcript": os.environ.get("TEST_TRANSCRIPT", "사람들 앞에 서면 다 망칠 것 같아요"),
            "confidence": float(os.environ.get("TEST_CONFIDENCE", "0.93")),
        },
    }

    req = urllib.request.Request(
        f"https://{gw}/v1/respond",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "x-api-key": key},
        method="POST",
    )

    events = []
    answer_parts = []
    token_count = 0

    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            typ = event.get("type")
            if typ != "token":
                print("EVENT =", typ)
            if typ == "meta":
                print("  primary =", event.get("primary"))
                print("  input_type =", (event.get("input") or {}).get("input_type"))
                print("  transcript =", ((event.get("input") or {}).get("stt") or {}).get("transcript"))
            elif typ == "chunks":
                print("  chunks =", [c.get("id") for c in event.get("chunks", [])])
            elif typ == "token":
                token_count += 1
                answer_parts.append(event.get("text", ""))
            elif typ == "done":
                break

    answer = "".join(answer_parts)
    print("TOKEN events =", token_count)
    print("ANSWER chars =", len(answer))
    print("ANSWER preview =")
    print(answer[:800])
    Path("api_contract_transcript_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("api_contract_transcript_answer.txt").write_text(answer, encoding="utf-8")
    print("SAVED api_contract_transcript_events.json")
    print("SAVED api_contract_transcript_answer.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
