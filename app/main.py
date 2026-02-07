from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import uuid
from datetime import datetime, timezone

def _db_url() -> str | None:
    return os.environ.get('DATABASE_URL')

def _spaces_cfg() -> dict[str,str] | None:
    keys = ['SPACES_ACCESS_KEY','SPACES_SECRET_KEY','SPACES_BUCKET','SPACES_REGION','SPACES_ENDPOINT']
    if not all(os.environ.get(k) for k in keys):
        return None
    return {k: os.environ[k] for k in keys}

def _spaces_public_base(cfg: dict[str,str]) -> str:
    # e.g. https://bucket.sfo3.digitaloceanspaces.com
    return f"https://{cfg['SPACES_BUCKET']}.{cfg['SPACES_ENDPOINT']}"

def _upload_png_to_spaces(img: bytes) -> str:
    cfg = _spaces_cfg()
    if not cfg:
        raise HTTPException(status_code=503, detail='Spaces not configured')

    import boto3
    from botocore.client import Config

    client = boto3.client(
        's3',
        region_name=cfg['SPACES_REGION'],
        endpoint_url=f"https://{cfg['SPACES_ENDPOINT']}",
        aws_access_key_id=cfg['SPACES_ACCESS_KEY'],
        aws_secret_access_key=cfg['SPACES_SECRET_KEY'],
        config=Config(signature_version='s3v4'),
    )

    now = datetime.now(timezone.utc)
    key = f"chargen/{now:%Y/%m/%d}/{uuid.uuid4().hex}.png"
    client.put_object(
        Bucket=cfg['SPACES_BUCKET'],
        Key=key,
        Body=img,
        ContentType='image/png',
        ACL='public-read',
        CacheControl='public, max-age=31536000, immutable',
    )
    return _spaces_public_base(cfg) + '/' + key

def _db_connect():
    url = _db_url()
    if not url:
        raise HTTPException(status_code=503, detail='DATABASE_URL not configured')
    import psycopg
    return psycopg.connect(url)

def _db_init():
    """Best-effort schema init."""
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists characters (
                  id uuid primary key,
                  created_at timestamptz not null,
                  name text not null,
                  race text,
                  class text,
                  mood text,
                  background text,
                  style text,
                  extra text,
                  traits text not null,
                  image_url text not null
                );
                """
            )
        conn.commit()


def _db_ensure():
    """Create tables if missing (handles first request after env changes)."""
    try:
        _db_init()
    except Exception:
        # still fail later with a clear error
        return


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
        "Framed like a chat profile picture. Aspect ratio 1:1 (square). High quality fantasy art. "
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

    # From UI
    name = (body.get("name") or "").strip()
    race = (body.get("race") or "").strip() or None
    clazz = (body.get("clazz") or "").strip() or None
    mood = (body.get("mood") or "").strip() or None
    bg = (body.get("bg") or "").strip() or None
    style = (body.get("style") or "").strip() or None
    extra = (body.get("extra") or "").strip() or None

    traits = (body.get("traits") or "").strip()
    if not traits:
        raise HTTPException(status_code=400, detail="missing traits")

    prompt = _build_prompt(traits)
    if name:
        prompt = prompt + f"\nCharacter name (for vibe only; do not write text): {name}\n"

    b64 = _gemini_generate_image_b64(prompt)
    img = base64.b64decode(b64)

    image_url = _upload_png_to_spaces(img)
    char_id = uuid.uuid4()

    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into characters
                  (id, created_at, name, race, class, mood, background, style, extra, traits, image_url)
                values
                  (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(char_id),
                    name or "Unnamed",
                    race,
                    clazz,
                    mood,
                    bg,
                    style,
                    extra,
                    traits,
                    image_url,
                ),
            )
        conn.commit()

    return Response(content=img, media_type="image/png")


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    t = _token_for_links(request)

    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select created_at, name, race, class, style, image_url from characters order by created_at desc limit 60"
            )
            rows = cur.fetchall()

    cards = []
    for created_at, name, race, clazz, style, image_url in rows:
        meta = " • ".join([x for x in [race or "", clazz or "", style or ""] if x])
        cards.append(
            "<div class='card'>"
            f"<a href='{image_url}' target='_blank' rel='noopener'>"
            f"<img src='{image_url}' loading='lazy' />"
            "</a>"
            f"<div class='cname'>{name}</div>"
            f"<div class='cmeta'>{meta}</div>"
            "</div>"
        )

    html = f"""<!doctype html>
<html><head>
<meta charset='utf-8' />
<meta name='viewport' content='width=device-width, initial-scale=1' />
<title>CharGen History</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:14px;}}
.topbar{{display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;}}
.grid{{display:grid; grid-template-columns:repeat(2, 1fr); gap:10px;}}
@media (min-width: 520px){{ .grid{{grid-template-columns:repeat(3, 1fr);}} }}
.card{{border:1px solid rgba(0,0,0,0.12); border-radius:12px; padding:8px;}}
.card img{{width:100%; aspect-ratio:1/1; object-fit:cover; border-radius:10px; background:#111;}}
.cname{{font-weight:600; margin-top:6px;}}
.cmeta{{opacity:0.7; font-size:12px;}}
a{{text-decoration:none; color:inherit;}}
</style>
</head>
<body>
  <div class='topbar'>
    <div><b>History</b></div>
    <div><a href='/?t={t}'>Back</a></div>
  </div>
  <div class='grid'>
    {''.join(cards) if cards else '<div style="opacity:0.7">No renders yet.</div>'}
  </div>
</body></html>"""

    return HTMLResponse(html)


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

    .top{{display:flex; gap:12px; align-items:flex-start; justify-content:center; max-width:520px; margin:0 auto;}}
    .styleBox{{width:200px; margin-top:10px;}}
    .radios{{display:flex; flex-direction:column; gap:8px;}}
    .radio{{display:flex; align-items:center; justify-content:flex-start; gap:10px; font-size:14px; padding:8px 10px; border:1px solid rgba(0,0,0,0.12); border-radius:10px; opacity:1;}}
    .radio input{{transform:scale(1.05); flex:0 0 auto;}}
    .radio span{{flex:1 1 auto; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#111; opacity:1;}}
    @media (max-width: 420px) {{ .top{{flex-direction:column; align-items:center;}} .styleBox{{width:100%; max-width:280px;}} }}

    .preview{{position:relative; width:100%; max-width:280px; margin:10px auto 12px;}}
    .preview::before{{content:''; display:block; padding-top:100%;}} /* 1:1 */
    .preview img{{position:absolute; inset:0; width:100%; height:100%; object-fit:cover; border-radius:12px; border:1px solid rgba(0,0,0,0.12); background:#111;}}
    .overlay{{position:absolute; inset:0; display:none; align-items:center; justify-content:center; flex-direction:column; gap:10px; border-radius:12px; background:rgba(0,0,0,0.35); color:#fff; font-size:14px;}}
    .spinner{{width:28px; height:28px; border:3px solid rgba(255,255,255,0.35); border-top-color:#fff; border-radius:50%; animation:spin 0.9s linear infinite;}}
    @keyframes spin{{to{{transform:rotate(360deg);}}}}

    .grid{{display:grid; grid-template-columns: 1fr 1fr; gap:10px; max-width:520px; margin:0 auto;}}
    @media (max-width: 420px) {{ .grid{{grid-template-columns:1fr;}} }}

    label{{display:block; font-size:12px; opacity:0.7; margin:0 0 6px;}}
    select{{width:100%; padding:10px; font-size:16px;}}
    input{{width:100%; padding:12px; font-size:16px; box-sizing:border-box; border-radius:10px; border:1px solid rgba(0,0,0,0.15);}}
    textarea{{width:100%; min-height:84px; padding:12px; font-size:16px; margin-top:10px; box-sizing:border-box; border-radius:10px; border:1px solid rgba(0,0,0,0.15);}}
    button{{padding:12px 16px; font-size:16px; margin-top:12px; width:100%;}}

    .actions{{max-width:520px; margin:0 auto;}}
    #dl{{display:none; margin-top:10px; text-align:center;}}
    #dl a{{display:inline-block; padding:10px 12px; border:1px solid rgba(0,0,0,0.15); border-radius:10px; text-decoration:none;}}
  </style>
</head>
<body>
  <h2>CharGen</h2>
  <div class="muted">Pick options + (optional) details. Tap Generate. <a href="/history?t={t}">History</a></div>

  <div class="top">
    <div class="preview">
      <img id="previewImg" alt="preview" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300' viewBox='0 0 300 300'><rect width='300' height='300' fill='%23151515'/><circle cx='150' cy='115' r='54' fill='%23222222'/><rect x='70' y='175' width='160' height='95' rx='18' fill='%23222222'/><text x='150' y='288' font-size='13' fill='%23888888' text-anchor='middle' font-family='Arial, sans-serif'>Avatar preview</text></svg>" />
      <div id="overlay" class="overlay">
        <div class="spinner"></div>
        <div>Generating…</div>
      </div>
    </div>

    <div class="styleBox">
      <label>Style</label>
      <div class="radios">
        <label class="radio"><input type="radio" name="style" value="Illustrated fantasy" checked /><span>Illustrated</span></label>
        <label class="radio"><input type="radio" name="style" value="Painterly" /><span>Painterly</span></label>
        <label class="radio"><input type="radio" name="style" value="Comic / cel shaded" /><span>Comic</span></label>
        <label class="radio"><input type="radio" name="style" value="Photoreal" /><span>Photoreal</span></label>
      </div>
    </div>
  </div>

  <div class="actions">
    <label>Character name (optional)</label>
    <input id="name" type="text" placeholder="Leave blank to auto-generate" />
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
  <div style="display:flex; gap:10px; margin-top:12px;">
    <button id="rand" type="button" style="margin-top:0;">Randomize</button>
    <button id="go" type="button" style="margin-top:0;">Generate</button>
  </div>
  <div id="dl"></div>
</div>

<script>
const token = {json.dumps(t)};
const btnRand = document.getElementById('rand');
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
  const style = (document.querySelector("input[name='style']:checked")?.value || '').trim();
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

function pickRandom(selectId) {{
  const el = document.getElementById(selectId);
  if (!el) return;
  const n = el.options.length;
  if (n <= 1) return;
  // include (any) where present
  el.selectedIndex = Math.floor(Math.random() * n);
}}

btnRand.onclick = () => {{
  pickRandom('race');
  pickRandom('clazz');
  // NOTE: do NOT randomize style (user must choose; default is Illustrated fantasy)
  pickRandom('mood');
  pickRandom('bg');
  // randomize name too
  const traits = buildTraits();
  document.getElementById('name').value = autoName(traits);
}};

function randFrom(arr) {{
  return arr[Math.floor(Math.random() * arr.length)];
}}

function autoName(seedText) {{
  // Lightweight fantasy-ish name generator.
  const a = ['Al','Bel','Cor','Da','El','Fa','Gal','Hel','Is','Ka','Lor','Mal','Nor','Or','Per','Quin','Ral','Ser','Tor','Ul','Val','Wyn','Yor','Zan'];
  const b = ['a','e','i','o','u','ae','ia','oi','ua','y'];
  const c = ['dor','rin','thas','wyn','mir','ion','rak','len','vash','gorn','bryn','syl','dun','mar','reth','zair','nox','lith','var','keth'];
  // try to be stable-ish per click by mixing seedText length
  const n = (seedText || '').length;
  const pick = (arr, k) => arr[(n + k + Math.floor(Math.random()*1000)) % arr.length];
  return pick(a,1) + pick(b,2) + pick(c,3);
}}

function safeFilename(s) {{
  return (s || 'avatar').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/(^-|-$)/g,'').slice(0,40) || 'avatar';
}}

btn.onclick = async () => {{
  dl.style.display = 'none';
  dl.innerHTML = '';
  setGenerating(true);
  btn.disabled = true;

  try {{
    const traits = buildTraits();
    let name = val('name');
    if (!name) name = autoName(traits);

    const resp = await fetch('/generate?t=' + encodeURIComponent(token), {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        traits,
        name,
        race: val('race'),
        clazz: val('clazz'),
        mood: val('mood'),
        bg: val('bg'),
        style: (document.querySelector("input[name='style']:checked")?.value || ''),
        extra: val('traits'),
      }})
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

    const fname = safeFilename(name) + '.png';
    dl.style.display = 'block';
    dl.innerHTML = `<a download="${{fname}}" href="${{url}}">⬇ Download</a>`;
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
