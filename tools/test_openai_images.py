#!/usr/bin/env python3
"""Local smoke test for OpenAI Images API.

Usage:
  OPENAI_API_KEY=... python tools/test_openai_images.py

Optional:
  PROMPT='...' SIZE='1024x1536'

Notes:
- This will create ./tmp_test.png when successful.
"""

import base64
import os
import sys
import json
import urllib.request


def main() -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("Missing OPENAI_API_KEY in environment.", file=sys.stderr)
        return 2

    prompt = os.environ.get(
        "PROMPT",
        "D&D style illustrated character portrait, half-elf ranger, emerald cloak, warm candlelit tavern background, friendly expression, high detail, fantasy art",
    )
    size = os.environ.get("SIZE", "1024x1536")  # 3:4 aspect

    # OpenAI Images API (JSON)
    body = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": size,
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return 1

    data = json.loads(raw)
    # Expected: data[0].b64_json
    b64 = None
    if isinstance(data, dict):
        arr = data.get("data") or []
        if arr and isinstance(arr[0], dict):
            b64 = arr[0].get("b64_json")

    if not b64:
        print("Unexpected response (no image payload). Raw:\n" + raw.decode("utf-8", "ignore"), file=sys.stderr)
        return 3

    img = base64.b64decode(b64)
    out = os.path.join(os.path.dirname(__file__), "..", "tmp_test.png")
    out = os.path.abspath(out)
    with open(out, "wb") as f:
        f.write(img)

    print("OK wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
