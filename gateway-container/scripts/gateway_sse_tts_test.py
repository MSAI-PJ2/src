#!/usr/bin/env python3
"""Collect /v1/respond SSE events with Azure TTS and save returned audio.

Required env:
- GW_FQDN
- API_KEY_VALUE
Optional env:
- TEST_SESSION_ID
- TEST_TEXT
- TEST_VOICE
- TEST_TTS_OUT
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
    out_path = Path(os.environ.get("TEST_TTS_OUT", "api_contract_tts_audio.wav"))
    payload = {
        "session_id": os.environ.get("TEST_SESSION_ID", "api-contract-tts-1"),
        "text": os.environ.get("TEST_TEXT", "사람들 앞에 서면 다 망칠 것 같아요"),
        "tts": {
            "enabled": True,
            "provider": "azure",
            "voice": os.environ.get("TEST_VOICE", "ko-KR-SunHiNeural"),
            "format": "wav",
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
    saved_audio = False

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
            if typ == "meta":
                print("  primary =", event.get("primary"))
            elif typ == "chunks":
                print("  chunks =", [c.get("id") for c in event.get("chunks", [])])
            elif typ == "token":
                token_count += 1
                answer_parts.append(event.get("text", ""))
            elif typ == "tts":
                print("  status =", event.get("status"))
                print("  provider =", event.get("provider"))
                audio_obj = event.get("audio") or {}
                mime_type = audio_obj.get("mime_type") or event.get("mime_type")
                audio_data = audio_obj.get("data") or event.get("audio_base64")
                print("  mime_type =", mime_type)
                print("  has_audio =", bool(audio_data))
                if audio_data:
                    out_path.write_bytes(base64.b64decode(audio_data))
                    saved_audio = True
                    print("  saved =", str(out_path))
                    print("  bytes =", out_path.stat().st_size)
            elif typ == "done":
                break

    answer = "".join(answer_parts)
    print("TOKEN events =", token_count)
    print("ANSWER chars =", len(answer))
    print("AUDIO saved =", saved_audio)
    Path("api_contract_tts_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("api_contract_tts_answer.txt").write_text(answer, encoding="utf-8")
    print("SAVED api_contract_tts_events.json")
    print("SAVED api_contract_tts_answer.txt")
    return 0 if saved_audio else 1


if __name__ == "__main__":
    raise SystemExit(main())
