from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

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
        return JSONResponse({"error": "CHARGEN_TOKEN not configured"}, status_code=503)

    token = _extract_token(request, request.headers.get("authorization"))
    if token != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

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
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        # Gemini often returns useful JSON error payloads.
        try:
            raw = e.read()
            msg = raw.decode("utf-8", "ignore")
        except Exception:
            msg = str(e)
        raise HTTPException(status_code=e.code, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"gemini request failed: {e}")

    if status >= 400:
        raise HTTPException(status_code=502, detail=f"gemini unexpected status {status}")

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
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:14px;}}
    h2{{margin:0 0 6px; font-size:18px;}}
    .muted{{opacity:0.7; font-size:13px; margin-bottom:10px;}}

    .preview{{position:relative; width:100%; max-width:360px; margin:10px auto 12px;}}
    .preview::before{{content:''; display:block; padding-top:133.333%;}} /* 3:4 */
    .preview img{{position:absolute; inset:0; width:100%; height:100%; object-fit:cover; border-radius:12px; border:1px solid rgba(0,0,0,0.12); background:#111;}}
    .overlay{{position:absolute; inset:0; display:none; align-items:center; justify-content:center; flex-direction:column; gap:10px; border-radius:12px; background:rgba(0,0,0,0.35); color:#fff; font-size:14px;}}
    .spinner{{width:28px; height:28px; border:3px solid rgba(255,255,255,0.35); border-top-color:#fff; border-radius:50%; animation:spin 0.9s linear infinite;}}
    @keyframes spin{{to{{transform:rotate(360deg);}}}}

    .grid{{display:grid; grid-template-columns: 1fr 1fr; gap:10px;}}
    @media (max-width: 420px) {{ .grid{{grid-template-columns:1fr;}} }}

    label{{display:block; font-size:12px; opacity:0.7; margin:0 0 6px;}}
    select{{width:100%; padding:10px; font-size:16px;}}
    textarea{{width:100%; min-height:84px; padding:12px; font-size:16px; margin-top:10px;}}
    button{{padding:12px 16px; font-size:16px; margin-top:12px; width:100%;}}

    .actions{{max-width:360px; margin:0 auto;}}
    #dl{{display:none; margin-top:10px; text-align:center;}}
    #dl a{{display:inline-block; padding:10px 12px; border:1px solid rgba(0,0,0,0.15); border-radius:10px; text-decoration:none;}}
  </style>
</head>
<body>
  <h2>CharGen</h2>
  <div class="muted">Pick options + (optional) details. Tap Generate.</div>

  <div class="preview">
    <img id="previewImg" alt="preview" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='300' height='400' viewBox='0 0 300 400'><rect width='300' height='400' fill='%23151515'/><circle cx='150' cy='150' r='58' fill='%23222222'/><rect x='70' y='230' width='160' height='110' rx='18' fill='%23222222'/><text x='150' y='360' font-size='14' fill='%23888888' text-anchor='middle' font-family='Arial, sans-serif'>Your avatar will appear here</text></svg>" />
    <div id="overlay" class="overlay">
      <div class="spinner"></div>
      <div>Generating…</div>
    </div>
  </div>

  <div class="grid">
  <div>
    <label>Race</label>
    <select id="race">
      <option value="">(any)</option>
      <option>Human</option>
      <option>Elf</option>
      <option>Half-elf</option>
      <option>Dwarf</option>
      <option>Halfling</option>
      <option>Tiefling</option>
      <option>Dragonborn</option>
      <option>Orc</option>
      <option>Gnome</option>
    </select>
  </div>

  <div>
    <label>Class</label>
    <select id="clazz">
      <option value="">(any)</option>
      <option>Fighter</option>
      <option>Wizard</option>
      <option>Rogue</option>
      <option>Cleric</option>
      <option>Paladin</option>
      <option>Ranger</option>
      <option>Warlock</option>
      <option>Bard</option>
      <option>Barbarian</option>
      <option>Druid</option>
      <option>Monk</option>
      <option>Sorcerer</option>
    </select>
  </div>

  <div>
    <label>Style</label>
    <select id="style">
      <option>Illustrated fantasy</option>
      <option>Painterly</option>
      <option>Comic / cel shaded</option>
      <option>Photoreal</option>
    </select>
  </div>

  <div>
    <label>Mood</label>
    <select id="mood">
      <option value="">(any)</option>
      <option>Calm</option>
      <option>Confident</option>
      <option>Friendly</option>
      <option>Stoic</option>
      <option>Menacing</option>
    </select>
  </div>

  <div>
    <label>Background</label>
    <select id="bg">
      <option>Simple / gradient</option>
      <option>Tavern</option>
      <option>Forest</option>
      <option>Castle</option>
      <option>Arcane library</option>
      <option>Battlefield haze</option>
    </select>
  </div>
</div>

<div class="actions">
  <textarea id="traits" placeholder="Optional details: hair, eyes, skin, armor/robe, weapon, colors, scars, accessories…"></textarea>
  <button id="go">Generate</button>
  <div id="dl"></div>
</div>

<script>
const token = {json.dumps(t)};
const btn = document.getElementById('go');
const previewImg = document.getElementById('previewImg');
const overlay = document.getElementById('overlay');
const dl = document.getElementById('dl');

function val(id) {{
  const el = document.getElementById(id);
  return (el && el.value || '').trim();
}}

function buildTraits() {{
  const race = val('race');
  const clazz = val('clazz');
  const style = val('style');
  const mood = val('mood');
  const bg = val('bg');
  const extra = val('traits');

  const parts = [];
  if (race) parts.push(race);
  if (clazz) parts.push(clazz);
  if (mood) parts.push(mood + ' expression');
  if (bg) parts.push(bg + ' background');
  if (style) parts.push('Style: ' + style);
  if (extra) parts.push(extra);
  return parts.join(', ');
}}

let lastObjectUrl = null;

function setGenerating(on) {{
  overlay.style.display = on ? 'flex' : 'none';
}}

btn.onclick = async () => {{
  dl.style.display = 'none';
  dl.innerHTML = '';
  setGenerating(true);
  btn.disabled = true;

  try {{
    const traits = buildTraits();
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
    if (lastObjectUrl) URL.revokeObjectURL(lastObjectUrl);
    lastObjectUrl = url;

    // reveal image in-place
    previewImg.src = url;

    dl.style.display = 'block';
    dl.innerHTML = `<a download="avatar.png" href="${{url}}">⬇ Download</a>`;
  }} catch (e) {{
    dl.style.display = 'block';
    dl.innerHTML = '<pre style="white-space:pre-wrap;color:#b00; margin:10px 0 0">' + String(e) + '</pre>';
  }} finally {{
    setGenerating(false);
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
