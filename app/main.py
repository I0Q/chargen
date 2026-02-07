from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="CharGen", version="0.0.2")


def _get_token() -> str | None:
    return os.environ.get("CHARGEN_TOKEN")


def _extract_token(request: Request, authorization: str | None) -> str | None:
    # Prefer Authorization: Bearer <token>
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    # Fallback: ?t=<token>
    t = request.query_params.get("t")
    return t.strip() if t else None


def _token_for_links(request: Request) -> str:
    # keep ?t=... in links so the UI works on phones
    t = request.query_params.get("t")
    return t or ""


@app.middleware("http")
async def token_gate(request: Request, call_next):
    # Always allow health.
    if request.url.path in ("/ping", "/robots.txt"):
        return await call_next(request)

    expected = _get_token()
    if not expected:
        # Fail-closed if not configured.
        raise HTTPException(status_code=503, detail="CHARGEN_TOKEN not configured")

    token = _extract_token(request, request.headers.get("authorization"))
    if token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    return await call_next(request)


@app.get("/ping")
def ping():
    return {"ok": True}


def _gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY")


def _gemini_model() -> str:
    # allow override without code changes
    return os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


def _build_prompt(traits: str) -> str:
    # Default: D&D illustrated portrait (not photoreal)
    return (
        "Create a Dungeons & Dragons style illustrated character avatar portrait. "
        "Framed like a chat profile picture. Aspect ratio 3:4. High quality fantasy art. "
        "No text, no watermark, no signature.\n\n"
        f"Character traits: {traits.strip()}\n"
    )


def _gemini_generate_image_b64(prompt: str) -> str:
    key = _gemini_key()
    if not key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    model = _gemini_model()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    payload: dict[str, Any] = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt,
                    }
                ]
            }
        ],
        # Try to request an image back. Some models ignore this; we parse response defensively.
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"gemini request failed: {e}")

    data = json.loads(raw)

    # Expected-ish shape: candidates[0].content.parts[*].inlineData|inline_data
    candidates = data.get("candidates") or []
    for cand in candidates:
        content = (cand or {}).get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return inline["data"]
            # Some responses nest as "inline_data": {"mime_type":..., "data":...}
            # Already handled above.

    raise HTTPException(status_code=502, detail="gemini did not return image data")


@app.post("/generate")
async def generate(request: Request):
    body = await request.json()
    traits = (body.get("traits") or "").strip()
    if not traits:
        raise HTTPException(status_code=400, detail="missing traits")

    prompt = _build_prompt(traits)
    b64 = _gemini_generate_image_b64(prompt)
    img = base64.b64decode(b64)

    return Response(content=img, media_type="image/png")


@app.get("/")
def index(request: Request):
    t = _token_for_links(request)
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CharGen</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:18px;}}
    textarea{{width:100%; min-height:160px; padding:12px; font-size:16px;}}
    button{{padding:12px 16px; font-size:16px; margin-top:10px;}}
    #out{{margin-top:16px;}}
    img{{max-width:100%; border-radius:12px;}}
    .muted{{opacity:0.7; font-size:13px;}}
  </style>
</head>
<body>
  <h2>CharGen (D&D Avatar)</h2>
  <div class="muted">Token-gated. No text/watermark. 3:4 portrait.</div>
  <textarea id="traits" placeholder="Ex: Female tiefling warlock, violet skin, gold eyes, short white hair, elegant robes, arcane book, candlelit library, confident smile"></textarea>
  <br/>
  <button id="go">Generate</button>
  <div id="out"></div>

<script>
const token = {json.dumps(t)};
const out = document.getElementById('out');
const btn = document.getElementById('go');
btn.onclick = async () => {{
  out.innerHTML = '<div class="muted">Generating…</div>';
  btn.disabled = true;
  try {{
    const traits = document.getElementById('traits').value;
    const resp = await fetch('/generate?t=' + encodeURIComponent(token), {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{traits}})
    }});
    if (!resp.ok) {{
      const txt = await resp.text();
      throw new Error(txt);
    }}
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    out.innerHTML = `<img src="${{url}}" />\n<div style="margin-top:10px"><a download="avatar.png" href="${{url}}">⬇ Download</a></div>`;
  }} catch (e) {{
    out.innerHTML = '<pre style="white-space:pre-wrap;color:#b00">' + String(e) + '</pre>';
  }} finally {{
    btn.disabled = false;
  }}
}};
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n"
