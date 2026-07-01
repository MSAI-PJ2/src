#!/usr/bin/env python3
"""Collect /v1/respond SSE events for audio STT input.

Required env:
- GW_FQDN
- API_KEY_VALUE
Optional env:
- TEST_AUDIO_PATH: default ~/stt_test_16k.wav
- TEST_SESSION_ID
- TEST_MIME_TYPE: default audio/wav
"""
import base64
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
    audio_path = Path(os.environ.get("TEST_AUDIO_PATH", "~/stt_test_16k.wav")).expanduser()
    if not audio_path.exists():
        print(f"ERROR: audio file not found: {audio_path}", file=sys.stderr)
        print("Set TEST_AUDIO_PATH or upload ~/stt_test_16k.wav first.", file=sys.stderr)
        sys.exit(2)

    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload = {
        "session_id": os.environ.get("TEST_SESSION_ID", "api-contract-audio-stt-1"),
        "input_type": "audio",
        "audio": {
            "kind": "base64",
            "data": audio_b64,
            "mime_type": os.environ.get("TEST_MIME_TYPE", "audio/wav"),
            "language": os.environ.get("TEST_LANGUAGE", "ko-KR"),
        },
        "stt": {
            "provider": "azure",
            "language": os.environ.get("TEST_LANGUAGE", "ko-KR"),
        },
    }

    req = urllib.request.Request(
        f"https://{gw}/v1/respond",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": key},
        method="POST",
    )

    events = []
    answer_parts = []
    token_count = 0

    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            typ = event.get("type")
            if typ != "token":
                print("EVENT =", typ)
            if typ == "stt":
                print("  status =", event.get("status"))
                print("  transcript =", event.get("transcript"))
                print("  error =", event.get("error") or event.get("reason"))
            elif typ == "meta":
                print("  primary =", event.get("primary"))
                print("  input_type =", (event.get("input") or {}).get("input_type"))
            elif typ == "chunks":
                print("  chunks =", [c.get("id") for c in event.get("chunks", [])])
            elif typ == "token":
                token_count += 1
                answer_parts.append(event.get("text", ""))
            elif typ == "input_required":
                print("  reason =", event.get("reason"))
                print("  message =", event.get("message"))
            elif typ == "done":
                break

    answer = "".join(answer_parts)
    print("TOKEN events =", token_count)
    print("ANSWER chars =", len(answer))
    print("ANSWER preview =")
    print(answer[:800])
    Path("api_contract_audio_stt_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("api_contract_audio_stt_answer.txt").write_text(answer, encoding="utf-8")
    print("SAVED api_contract_audio_stt_events.json")
    print("SAVED api_contract_audio_stt_answer.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
