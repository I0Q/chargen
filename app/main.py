from __future__ import annotations

import base64
import json
import os
import time
import hashlib
import hmac
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse

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


def _make_png_thumbnail(img: bytes, *, size: int = 256) -> bytes:
    # Square thumbnail (preserves aspect by cover-cropping, then resize).
    from PIL import Image
    import io

    im = Image.open(io.BytesIO(img)).convert('RGBA')
    w, h = im.size
    # center crop to square
    m = min(w, h)
    left = (w - m) // 2
    top = (h - m) // 2
    im = im.crop((left, top, left + m, top + m))
    im = im.resize((size, size), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    im.save(out, format='PNG', optimize=True)
    return out.getvalue()


def _upload_png_and_thumb_to_spaces(img: bytes) -> tuple[str, str | None]:
    """Upload full PNG and a small thumbnail PNG. Returns (image_url, thumb_url)."""
    # Upload original
    image_url = _upload_png_to_spaces(img, key_prefix='chargen')

    # Upload thumb (best-effort)
    try:
        thumb = _make_png_thumbnail(img, size=256)
        thumb_url = _upload_png_to_spaces(thumb, key_prefix='chargen-thumbs')
    except Exception:
        thumb_url = None

    return image_url, thumb_url

def _upload_png_to_spaces(img: bytes, *, key_prefix: str = 'chargen') -> str:
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
    key = f"{key_prefix}/{now:%Y/%m/%d}/{uuid.uuid4().hex}.png"
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
                  gender text,
                  style text,
                  extra text,
                  traits text not null,
                  image_url text not null,
                  thumb_url text,
                  quote text
                );

                -- lightweight migration for older rows
                alter table characters add column if not exists quote text;
                alter table characters add column if not exists gender text;
                alter table characters add column if not exists thumb_url text;
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


app = FastAPI(title="CharGen", version="0.0.4")

# -------------------- auth (token + passphrase session) --------------------
# Token (phone-friendly): CHARGEN_TOKEN via ?t=... or Authorization: Bearer ...
# Passphrase (browser-friendly): PASSPHRASE_SHA256 (hex) with HttpOnly cookie session.

PASSPHRASE_SHA256 = (os.environ.get('PASSPHRASE_SHA256') or '').strip().lower()
SESSION_TTL_SEC = 24 * 60 * 60


def _get_token() -> str | None:
    return os.environ.get("CHARGEN_TOKEN")


def _sign_session(ts: int) -> str:
    # Stateless session cookie: <ts>.<hmac>
    key = PASSPHRASE_SHA256.encode('utf-8')
    msg = str(ts).encode('utf-8')
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _is_session_authed(request: Request) -> bool:
    v = request.cookies.get('cg_sid')
    if not v:
        return False
    try:
        ts_s, sig = v.split('.', 1)
        ts = int(ts_s)
    except Exception:
        return False

    now = int(time.time())
    if ts > now + 60:
        return False
    if (now - ts) > SESSION_TTL_SEC:
        return False

    expected = _sign_session(ts).split('.', 1)[1]
    return hmac.compare_digest(sig, expected)


def _login_html(err: str = '') -> str:
    # RNG-style login UI (topbar + centered card)
    err_html = ''
    if err:
        esc = (err.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'))
        err_html = f'<div class="err">{esc}</div>'

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>CharGen — Login</title>
  <style>
    :root{{--pad:40px;--maxw:860px;--topbar-h:56px;--radius:18px;--shadow:0 16px 50px rgba(0,0,0,0.10)}}
    @media (max-width:420px){{:root{{--pad:20px;--topbar-h:52px}}}}
    *{{box-sizing:border-box}}
    body{{margin:0;color:#111;font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:
      radial-gradient(900px 280px at 20% 0%, rgba(106,90,205,0.12), transparent 60%),
      radial-gradient(900px 280px at 80% 20%, rgba(0,188,212,0.10), transparent 60%),
      #ffffff;
    }}
    .topbar{{position:fixed;top:0;left:0;right:0;height:var(--topbar-h);display:flex;align-items:center;z-index:1000;
      background:rgba(255,255,255,0.88);backdrop-filter:blur(10px);border-bottom:1px solid rgba(0,0,0,0.06);
    }}
    .topbarInner{{max-width:var(--maxw);width:100%;margin:0 auto;padding:0 var(--pad);display:flex;align-items:center;justify-content:space-between;gap:12px}}
    .brand{{font-weight:700;letter-spacing:0.2px;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    @media (max-width:420px){{.brand{{font-size:15px}}}}
    main{{padding-top:calc(var(--topbar-h) + 18px);padding-left:var(--pad);padding-right:var(--pad);padding-bottom:40px}}
    .pageCenter{{max-width:720px;margin:0 auto}}
    .h1{{font-size:20px;font-weight:800; letter-spacing:0.1px; margin:0 0 12px}}
    .card{{background:rgba(255,255,255,0.92);border:1px solid rgba(0,0,0,0.10);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px}}
    label{{display:block;font-size:12px;opacity:0.75;margin:0 0 6px}}
    input{{width:100%;padding:12px;font-size:16px;box-sizing:border-box;border-radius:12px;border:1px solid rgba(0,0,0,0.15);background:#fff}}
    button{{margin-top:12px;width:100%;padding:12px 16px;font-size:16px;border-radius:12px;border:1px solid rgba(0,0,0,0.08);background:#0A60FF;color:#fff;font-weight:800}}
    .err{{margin-top:10px; color:#b00020; font-size:14px; font-weight:600}}
  </style>
</head>
<body>
  <div class=\"topbar\"><div class=\"topbarInner\"><div class=\"brand\">CharGen</div><div style=\"width:80px\"></div></div></div>
  <main>
    <div class=\"pageCenter\">
      <div class=\"h1\">Enter passphrase</div>
      <div class=\"card\">
        <form method=\"post\" action=\"/login\">
          <label for=\"pass\">Passphrase</label>
          <input id=\"pass\" name=\"passphrase\" type=\"password\" placeholder=\"Passphrase\" autofocus required />
          <button type=\"submit\">Unlock</button>
          {err_html}
        </form>
      </div>
    </div>
  </main>
</body>
</html>"""


def _extract_token(request: Request, authorization: str | None) -> str | None:
    # Prefer Authorization: Bearer <token>
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    # Fallback: ?t=<token>
    t = request.query_params.get("t")
    return t.strip() if t else None


def _wants_html(request: Request) -> bool:
    accept = (request.headers.get('accept') or '').lower()
    return 'text/html' in accept or accept == ''


def _token_for_links(request: Request) -> str:
    # keep ?t=... in links so the UI works on phones
    t = request.query_params.get("t")
    return t or ""


@app.middleware("http")
async def token_gate(request: Request, call_next):

    # Always allow health.
    if request.url.path in ("/ping", "/robots.txt"):
        return await call_next(request)

    # Allow login/logout endpoints for passphrase flow.
    if request.url.path in ("/login", "/logout"):
        return await call_next(request)

    expected_token = _get_token()
    if not expected_token:
        # Fail-closed if not configured.
        return JSONResponse({"error": "CHARGEN_TOKEN not configured"}, status_code=503)

    # Auth path A: bearer/query token
    token = _extract_token(request, request.headers.get("authorization"))
    if token == expected_token:
        return await call_next(request)

    # Auth path B: passphrase session cookie (optional)
    if PASSPHRASE_SHA256 and _is_session_authed(request):
        return await call_next(request)

    # Not authorized
    if _wants_html(request):
        if PASSPHRASE_SHA256 and len(PASSPHRASE_SHA256) == 64:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(
            "<html><body style='font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; padding:16px'>"
            "<h2 style='margin:0 0 8px'>Unauthorized</h2>"
            "<div style='opacity:0.75'>This app requires a token link (?t=...) or passphrase auth to be enabled on the server.</div>"
            "</body></html>",
            status_code=401,
        )
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.get("/ping")
def ping():
    return {"ok": True}


@app.get("/api/whoami")
def whoami(request: Request):
    """Debug helper: tells which auth path was used (token vs passphrase cookie)."""
    expected_token = _get_token() or ''
    tok = _extract_token(request, request.headers.get('authorization'))
    if tok and tok == expected_token:
        return {"ok": True, "auth": "token"}
    if PASSPHRASE_SHA256 and _is_session_authed(request):
        return {"ok": True, "auth": "passphrase"}
    # if middleware is bypassed somehow, still indicate
    raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/login")
def login_get(request: Request):
    # If passphrase auth not enabled, show a clear message (but don't 503, to avoid platform error pages).
    if not PASSPHRASE_SHA256 or len(PASSPHRASE_SHA256) != 64:
        resp = HTMLResponse(
            "<html><body style='font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; padding:16px'>"
            "<h2 style='margin:0 0 8px'>Passphrase login not enabled</h2>"
            "<div style='opacity:0.75'>Server is not configured with PASSPHRASE_SHA256. Use your token link (?t=...) instead.</div>"
            "</body></html>",
            status_code=200,
        )
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    if _is_session_authed(request):
        resp = RedirectResponse(url="/", status_code=302)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    resp = HTMLResponse(_login_html(err=str(request.query_params.get('err') or '')))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.post("/login")
async def login_post(request: Request):
    if not PASSPHRASE_SHA256 or len(PASSPHRASE_SHA256) != 64:
        resp = RedirectResponse(url="/login", status_code=302)
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    form = await request.form()
    passphrase = str(form.get('passphrase') or '')
    digest = hashlib.sha256(passphrase.encode('utf-8')).hexdigest()

    # constant-time compare
    ok = False
    try:
        ok = hmac.compare_digest(digest.lower(), PASSPHRASE_SHA256)
    except Exception:
        ok = False

    if not ok:
        # small delay to slow brute force
        time.sleep(0.35)
        resp = RedirectResponse(url="/login?err=Wrong%20passphrase", status_code=302)
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    ts = int(time.time())
    sid = _sign_session(ts)

    resp = RedirectResponse(url="/", status_code=302)
    resp.headers['Cache-Control'] = 'no-store'
    resp.set_cookie(
        key='cg_sid',
        value=sid,
        max_age=SESSION_TTL_SEC,
        httponly=True,
        secure=True,
        samesite='lax',
        path='/',
    )
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=302)
    resp.headers['Cache-Control'] = 'no-store'
    resp.delete_cookie('cg_sid', path='/')
    return resp


@app.post("/api/character/{cid}/regenerate")
async def character_regenerate(cid: str, request: Request):
    body = await request.json()
    new_extra = (body.get("extra") or "").strip() or None
    new_traits = (body.get("traits") or "").strip() or None
    # style can be overridden by caller, but is optional (traits field is source of truth)
    new_style = (body.get("style") or "").strip() or None

    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select race, class, mood, background, gender, style, extra, traits, image_url from characters where id=%s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            race, clazz, mood, bg, gender, style, old_extra, old_traits, old_image_url = row

    # Allow style override from UI
    if new_style is not None:
        style = new_style

    # Use same metadata; only extra changes (from UI). Traits drives regen (user-editable).
    extra = new_extra if new_extra is not None else old_extra
    traits = new_traits if new_traits is not None else old_traits

    # If user cleared traits, rebuild it from metadata+extra.
    if not traits:
        traits = _compose_traits(race, clazz, mood, bg, gender, style, extra)

    # Ensure selected style influences the generation. If caller didn't include a Style: tag,
    # append one so _build_prompt sees it.
    if style and ("style:" not in (traits or "").lower()):
        traits = (traits or "").strip()
        traits = (traits + (", " if traits else "") + f"Style: {style}")

    prompt = _build_prompt(traits)

    b64 = _gemini_generate_image_b64(prompt)
    img = base64.b64decode(b64)

    image_url, thumb_url = _upload_png_and_thumb_to_spaces(img)

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update characters set extra=%s, traits=%s, style=%s, image_url=%s, thumb_url=%s where id=%s",
                (extra, traits, style, image_url, thumb_url, cid),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()

    # Best-effort cleanup of old image
    _delete_spaces_object_if_ours(old_image_url)

    return {"ok": True, "image_url": image_url, "thumb_url": thumb_url}


@app.post("/api/character/{cid}/delete")
async def character_delete(cid: str, request: Request):
    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select image_url from characters where id=%s", (cid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            (image_url,) = row
            cur.execute("delete from characters where id=%s", (cid,))
        conn.commit()

    _delete_spaces_object_if_ours(image_url)
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


def _compose_traits(
    race: str | None,
    clazz: str | None,
    mood: str | None,
    bg: str | None,
    gender: str | None,
    style: str | None,
    extra: str | None,
) -> str:
    parts: list[str] = []
    if race:
        parts.append(race)
    if clazz:
        parts.append(clazz)
    if mood:
        parts.append(mood + " expression")
    if bg:
        parts.append(bg + " background")
    if gender:
        parts.append(gender)
    if style:
        parts.append("Style: " + style)
    if extra:
        parts.append(extra)
    return ", ".join([p for p in parts if p])


def _spaces_client(cfg: dict[str, str]):
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        region_name=cfg["SPACES_REGION"],
        endpoint_url=f"https://{cfg['SPACES_ENDPOINT']}",
        aws_access_key_id=cfg["SPACES_ACCESS_KEY"],
        aws_secret_access_key=cfg["SPACES_SECRET_KEY"],
        config=Config(signature_version="s3v4"),
    )


def _delete_spaces_object_if_ours(url: str | None):
    cfg = _spaces_cfg()
    if not cfg or not url:
        return
    base = _spaces_public_base(cfg).rstrip("/") + "/"
    if not url.startswith(base):
        return
    key = url[len(base) :]
    try:
        client = _spaces_client(cfg)
        client.delete_object(Bucket=cfg["SPACES_BUCKET"], Key=key)
    except Exception:
        return


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
    gender = (body.get("gender") or "").strip() or None
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

    image_url, thumb_url = _upload_png_and_thumb_to_spaces(img)
    char_id = uuid.uuid4()

    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into characters
                  (id, created_at, name, race, class, mood, background, gender, style, extra, traits, image_url, thumb_url)
                values
                  (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(char_id),
                    name or "Unnamed",
                    race,
                    clazz,
                    mood,
                    bg,
                    gender,
                    style,
                    extra,
                    traits,
                    image_url,
                    thumb_url,
                ),
            )
        conn.commit()

    return Response(content=img, media_type="image/png")




@app.get("/history")
def history_redirect(request: Request):
    t = _token_for_links(request)
    return HTMLResponse(f"<meta http-equiv='refresh' content='0; url=/characters?t={t}'>")

@app.get("/characters", response_class=HTMLResponse)
def characters(request: Request):
    t = _token_for_links(request)

    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id, created_at, name, race, class, gender, style, extra, traits, image_url, thumb_url from characters order by created_at desc limit 60"
            )
            rows = cur.fetchall()

    cards = []
    for cid, created_at, name, race, clazz, gender, style, extra, traits, image_url, thumb_url in rows:
        meta = " • ".join([x for x in [race or "", clazz or "", gender or "", style or ""] if x])
        edit_url = f"/c/{cid}?t={t}"
        thumb = thumb_url or image_url
        cards.append(
            "<div class='card'>"
            f"<a href='{edit_url}'>"
            f"<img src='{thumb}' loading='lazy' />"
            "</a>"
            f"<div class='cname'><a href='{edit_url}'>{name}</a></div>"
            f"<div class='cmeta'>{meta}</div>"
            "</div>"
        )

    html = f"""<!doctype html>
<html><head>
<meta charset='utf-8' />
<meta name='viewport' content='width=device-width, initial-scale=1' />
<title>CharGen Characters</title>
<style>
html, body{{height:100%;}}
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:0;}}
.header{{display:flex; justify-content:space-between; align-items:center; padding:16px 14px; border-bottom:1px solid rgba(0,0,0,0.12);}}
.header .title{{font-size:20px; font-weight:700;}}
.header a{{text-decoration:none;}}
.main{{padding:14px;}}
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
  <div class='header'><div class='title'>Characters</div><div><a href='/?t={t}'>Generate a new character</a></div></div><div class='main'><div class='grid'>
    {''.join(cards) if cards else '<div style="opacity:0.7">No renders yet.</div>'}
  </div></div>
</body></html>"""

    return HTMLResponse(html)




@app.get("/c/{cid}", response_class=HTMLResponse)
def character_page(cid: str, request: Request):
    t = _token_for_links(request)
    _db_ensure()

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id, created_at, name, race, class, mood, background, style, extra, traits, image_url from characters where id = %s",
                (cid,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="not found")

    (cid, created_at, name, race, clazz, mood, bg, style, extra, traits, image_url) = row

    def esc(s: str | None) -> str:
        if not s:
            return ""
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    html = f"""<!doctype html>
<html><head>
<meta charset='utf-8' />
<meta name='viewport' content='width=device-width, initial-scale=1' />
<title>{esc(name) or 'Character'}</title>
<style>
html, body{{height:100%;}}
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:0; padding:0 0 120px 0;}}
.topbar{{display:flex; justify-content:space-between; align-items:center; padding:16px 14px; border-bottom:1px solid rgba(0,0,0,0.12);}}
.topbar b{{font-size:20px;}}
.wrap{{max-width:520px; margin:0 auto; padding:14px;}}
img{{display:block; width:100%; max-width:320px; margin:0 auto; aspect-ratio:1/1; object-fit:cover; border-radius:12px; border:1px solid rgba(0,0,0,0.12); background:#111;}}
label{{display:block; font-size:12px; opacity:0.7; margin:12px 0 6px;}}
input, textarea{{width:100%; box-sizing:border-box; padding:12px; font-size:16px; border-radius:10px; border:1px solid rgba(0,0,0,0.15);}}
textarea{{min-height:120px;}}
button{{padding:12px 16px; font-size:16px; margin-top:12px; width:100%;}}
.muted{{opacity:0.7; font-size:13px;}}
#msg{{margin-top:10px;}}

/* Floating save bar */
.savebar{{position:fixed; left:0; right:0; bottom:0; background:rgba(255,255,255,0.96); border-top:1px solid rgba(0,0,0,0.12); padding:10px 14px;}}
.savebar .inner{{max-width:520px; margin:0 auto; display:flex; gap:10px; align-items:center;}}
.savebar button{{margin-top:0; background:#0A60FF; color:#fff; border:1px solid rgba(0,0,0,0.08); border-radius:12px;}}
.savebar button:disabled{{opacity:0.7;}}
</style>
</head>
<body>
    <div class='topbar'>
      <div><b>{esc(name) or 'Character'}</b></div>
      <div><a href='/characters?t={t}'>Characters</a></div>
    </div>

  <div class='wrap'>

    <a href='{esc(image_url)}' target='_blank' rel='noopener' style='display:block;'>
      <div style='position:relative; display:block;'>
        <img id='mainimg' src='{esc(image_url)}' style='display:block; width:100%; border-radius:12px;' />
        <div id='imgOverlay' style='display:none; position:absolute; inset:0; border-radius:12px; background:rgba(0,0,0,0.28); align-items:center; justify-content:center; flex-direction:column; gap:10px; color:#fff;'>
          <div style='width:28px;height:28px;border:3px solid rgba(255,255,255,0.35);border-top-color:#fff;border-radius:50%; animation:spin 0.9s linear infinite;'></div>
          <div style='font-size:14px; opacity:0.95;'>Regenerating…</div>
        </div>
      </div>
    </a>

    <div style='display:flex; gap:10px; margin-top:10px;'>
      <a id='dlimg' href='{esc(image_url)}' download style='flex:1; text-align:center; padding:10px 12px; border:1px solid rgba(0,0,0,0.15); border-radius:10px;'>⬇ Download image</a>
      <button id='dldetails' type='button' style='flex:1; margin-top:0;'>⬇ Download details</button>
    </div>

    <div style='display:flex; gap:10px; margin-top:10px;'>
      <button id='regen' type='button' style='flex:1; margin-top:0;'>🔁 Regenerate image</button>
      <button id='del' type='button' style='flex:1; margin-top:0; border:1px solid rgba(180,0,0,0.35);'>🗑 Delete</button>
    </div>

    <div class='muted' style='margin-top:8px;'>ID: {esc(str(cid))}</div>

    <label>Name</label>
    <input id='name' value="{esc(name)}" />

    <label>Details (notes)</label>
    <textarea id='extra'>{esc(extra)}</textarea>

    <button id='genquote' type='button' style='margin-top:10px;'>✨ Generate Quote</button>

    <label>Traits string (affects image generation)</label>
    <textarea id='traits'>{esc(traits)}</textarea>

    <div id='msg' class='muted'></div>
  </div>

  <div class='savebar'>
    <div class='inner'>
      <button id='save' type='button' style='flex:1;'>Save</button>
    </div>
  </div>

<script>
const token = {t!r};
const cid = {str(cid)!r};
const msg = document.getElementById('msg');
const previewImg = document.getElementById('mainimg');
const imgOverlay = document.getElementById('imgOverlay');
const dlimg = document.getElementById('dlimg');
const btnRegen = document.getElementById('regen');
const btnDel = document.getElementById('del');

function downloadText(filename, text) {{
  const blob = new Blob([text], {{type:'text/plain'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}}

document.getElementById('dldetails').onclick = () => {{
  const name = document.getElementById('name').value || 'Unnamed';
  const details = document.getElementById('extra').value || '';
  const traits = document.getElementById('traits').value || '';
  const lines = [];
  lines.push(`Name: ${{name}}`);
  lines.push(`ID: ${{cid}}`);
  lines.push(`Image: ${{document.getElementById('dlimg').href}}`);
  lines.push('');
  lines.push('Details:');
  lines.push(details);
  lines.push('');
  lines.push('Traits:');
  lines.push(traits);
  const fname = (name || 'character').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/(^-|-$)/g,'').slice(0,40) || 'character';
  downloadText(fname + '.txt', lines.join('\\n'));
}};

const btnSave = document.getElementById('save');
const btnGen = document.getElementById('genquote');

async function postJson(url, payload) {{
  const resp = await fetch(url, {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify(payload),
  }});
  const txt = await resp.text();
  if (!resp.ok) throw new Error(`HTTP ${{resp.status}}: ${{txt}}`);
  try {{ return JSON.parse(txt); }} catch {{ return {{ok:true}}; }}
}}

// Make JS failures visible on mobile.
window.addEventListener('error', (e) => {{
  try {{ msg.textContent = 'JS error: ' + (e?.message || String(e)); }} catch {{}}
}});
window.addEventListener('unhandledrejection', (e) => {{
  try {{ msg.textContent = 'Promise error: ' + String(e?.reason || e); }} catch {{}}
}});

(async () => {{
  try {{
    const r = await fetch(`/api/whoami?t=${{encodeURIComponent(token)}}`);
    if (r.ok) {{
      const j = await r.json();
      if (j && j.auth) msg.textContent = `Authed via: ${{j.auth}}`;
    }}
  }} catch {{}}
}})();

btnGen.onclick = async () => {{
  btnGen.disabled = true;
  msg.textContent = 'Generating quote…';
  try {{
    const out = await postJson(`/api/character/${{cid}}/quote?t=${{encodeURIComponent(token)}}`, {{}});
    if (out && out.quote) {{
      const extra = document.getElementById('extra');
      const q = out.quote;
      const current = extra.value || '';
      // Append to details as a new line.
      extra.value = (current ? (current + '\\n\\n') : '') + 'Quote: ' + q;
    }}
    msg.textContent = 'Quote added to details.';
  }} catch (e) {{
    msg.textContent = 'Error: ' + String(e);
  }} finally {{
    btnGen.disabled = false;
  }}
}};

btnRegen.onclick = async () => {{
  btnRegen.disabled = true;
  msg.textContent = 'Regenerating image…';
  if (imgOverlay) imgOverlay.style.display = 'flex';
  try {{
    // Use whatever the user currently has in Traits (source of truth).
    const payload = {{
      extra: document.getElementById('extra').value,
      traits: document.getElementById('traits').value,
    }};
    const out = await postJson(`/api/character/${{cid}}/regenerate?t=${{encodeURIComponent(token)}}`, payload);
    if (out && out.image_url) {{
      previewImg.src = out.image_url;
      dlimg.href = out.image_url;
    }}
    msg.textContent = 'Image regenerated.';
  }} catch (e) {{
    msg.textContent = 'Error: ' + String(e);
  }} finally {{
    if (imgOverlay) imgOverlay.style.display = 'none';
    btnRegen.disabled = false;
  }}
}};

btnDel.onclick = async () => {{
  if (!confirm('Delete this character?')) return;
  btnDel.disabled = true;
  msg.textContent = 'Deleting…';
  try {{
    await postJson(`/api/character/${{cid}}/delete?t=${{encodeURIComponent(token)}}`, {{}});
    window.location.href = `/characters?t=${{encodeURIComponent(token)}}`;
  }} catch (e) {{
    msg.textContent = 'Error: ' + String(e);
    btnDel.disabled = false;
  }}
}};

btnSave.onclick = async () => {{
  // simple save animation
  const old = btnSave.textContent;
  btnSave.disabled = true;
  btnSave.textContent = 'Saving…';
  msg.textContent = '';

  const payload = {{
    name: document.getElementById('name').value,
    extra: document.getElementById('extra').value,
    traits: document.getElementById('traits').value,
  }};

  try {{
    await postJson(`/api/character/${{cid}}?t=${{encodeURIComponent(token)}}`, payload);
    // redirect back to list
    window.location.href = `/characters?t=${{encodeURIComponent(token)}}`;
  }} catch (e) {{
    msg.textContent = 'Error: ' + String(e);
  }} finally {{
    btnSave.disabled = false;
    btnSave.textContent = old;
  }}
}};
</script>
</div>
</body></html>"""

    return HTMLResponse(html)


@app.post("/api/character/{cid}/quote")
async def character_generate_quote(cid: str, request: Request):
    # Generate a short in-character quote based on stored traits.
    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select name, race, class, mood, background, style, extra, traits from characters where id=%s",
                (cid,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    name, race, clazz, mood, bg, style, extra, traits = row

    prompt = (
        "Write ONE short quote (max 25 words) that this fantasy RPG character would say. "
        "First-person voice. No quote marks. No emojis. No modern references. "
        "Do not include the character's name unless it would naturally be spoken.\n\n"
        f"Name: {name}\n"
        f"Race: {race}\nClass: {clazz}\nMood: {mood}\nBackground: {bg}\nStyle: {style}\n"
        f"Details: {extra}\nTraits: {traits}\n"
    )

    # Use Gemini text generation (same API; image model key already configured).
    # We call generateContent and extract first text part.
    key = _gemini_key()
    if not key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    model = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        raw = e.read()
        raise HTTPException(status_code=e.code, detail=raw.decode("utf-8", "ignore"))

    data = json.loads(raw)
    text_out = None
    for cand in (data.get("candidates") or []):
        content = (cand or {}).get("content") or {}
        for part in (content.get("parts") or []):
            if isinstance(part, dict) and part.get("text"):
                text_out = part["text"].strip()
                break
        if text_out:
            break

    if not text_out:
        raise HTTPException(status_code=502, detail="no text returned")

    # Keep it single-line.
    text_out = " ".join(text_out.split())

    return {"quote": text_out}


@app.post("/api/character/{cid}")
async def character_update(cid: str, request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip() or None
    extra = (body.get("extra") or "").strip() or None
    traits = (body.get("traits") or "").strip() or None

    if not name:
        raise HTTPException(status_code=400, detail="missing name")

    _db_ensure()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update characters set name=%s, extra=%s, traits=%s where id=%s",
                (name, extra, traits, cid),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=404, detail="not found")
        conn.commit()

    return {"ok": True}
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
    html, body{{height:100%;}}
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:0;}}

    .header{{display:flex; justify-content:space-between; align-items:center; padding:16px 14px; border-bottom:1px solid rgba(0,0,0,0.12);}}
    .header .title{{font-size:20px; font-weight:700;}}
    .main{{padding:14px;}}

    .muted{{opacity:0.7; font-size:13px; margin-bottom:10px;}}

    .top{{display:flex; gap:12px; align-items:flex-start; justify-content:center; max-width:520px; margin:0 auto;}}
    .styleBox{{width:200px; margin-top:10px;}}
    .radios{{display:flex; flex-direction:column; gap:8px;}}
    .radio{{display:grid; grid-template-columns:26px 1fr; align-items:center; gap:10px; font-size:14px; padding:10px 10px; border:1px solid rgba(0,0,0,0.12); border-radius:10px; background:#fff; color:#111; opacity:1 !important;}}
    .radio input{{transform:scale(1.05); margin:0;}}
    .radio .rtext{{display:block; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#111; opacity:1;}}
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
    button.primary{{background:#0A60FF; color:#fff; border:1px solid rgba(0,0,0,0.08); border-radius:12px;}}
    button.primary:disabled{{opacity:0.7;}}

    /* removed full-screen progress */
    /* #full removed */
    /* #full .box removed */
    /* #full .bar removed */
    /* #full .bar > div removed */
    /* #full .label removed */

    .actions{{max-width:520px; margin:0 auto;}}
  </style>
</head>
<body>
  <div class="header"><div class="title">CharGen</div><div><a href="/characters?t={t}">Characters</a></div></div><div class="main"><div class="muted">Pick options + (optional) details. Tap Generate.</div>

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
        <label class="radio"><input type="radio" name="style" value="Illustrated fantasy" checked data-preview="https://storyforge-assets.sfo3.digitaloceanspaces.com/chargen/placeholders/apple_illustrated.png" /><span class="rtext">Illustrated</span></label>
        <label class="radio"><input type="radio" name="style" value="Flat vector" data-preview="https://storyforge-assets.sfo3.digitaloceanspaces.com/chargen/placeholders/apple_vector.png" /><span class="rtext">Flat vector</span></label>
        <label class="radio"><input type="radio" name="style" value="Comic / cel shaded" data-preview="https://storyforge-assets.sfo3.digitaloceanspaces.com/chargen/placeholders/apple_comic.png" /><span class="rtext">Comic</span></label>
        <label class="radio"><input type="radio" name="style" value="Photoreal" data-preview="https://storyforge-assets.sfo3.digitaloceanspaces.com/chargen/placeholders/apple_photoreal.png" /><span class="rtext">Photoreal</span></label>
      </div>
      <div class="muted" style="margin-top:8px;">Tap a style to preview.</div>
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
    <label>Gender</label>
    <select id="gender">
      <option value="">(any)</option>
      <option>Female</option>
      <option>Male</option>
      <option>Non-binary</option>
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
    <button id="go" class="primary" type="button" style="margin-top:0;">Generate</button>
  </div>
</div>

<script>
const token = {json.dumps(t)};
const btnRand = document.getElementById('rand');
const btn = document.getElementById('go');
const previewImg = document.getElementById('previewImg');
const overlay = document.getElementById('overlay');

function setStylePreview() {{
  const r = document.querySelector("input[name='style']:checked");
  const src = r ? r.dataset.preview : '';
  if (src) previewImg.src = src;
}}

document.querySelectorAll("input[name='style']").forEach(r => {{
  r.addEventListener('change', setStylePreview);
  r.addEventListener('click', setStylePreview);
  r.addEventListener('touchend', setStylePreview);
}});
setStylePreview();



function val(id) {{
  const el = document.getElementById(id);
  return (el && el.value || '').trim();
}}

function buildTraits() {{
  const race = val('race');
  const clazz = val('clazz');
  const style = (document.querySelector("input[name='style']:checked")?.value || '').trim();
  const mood = val('mood');
  const gender = val('gender');
  const bg = val('bg');
  const extra = val('traits');

  const parts = [];
  if (race) parts.push(race);
  if (clazz) parts.push(clazz);
  if (mood) parts.push(mood + ' expression');
  if (gender) parts.push(gender);
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

function doRandomize() {{
  pickRandom('race');
  pickRandom('clazz');
  // NOTE: do NOT randomize style (user must choose; default is Illustrated fantasy)
  pickRandom('mood');
  pickRandom('gender');
  pickRandom('bg');
  // randomize name too
  const traits = buildTraits();
  const nameEl = document.getElementById('name');
  if (nameEl) nameEl.value = autoName(traits);
}}

btnRand.onclick = doRandomize;
btnRand.addEventListener('touchend', (e) => {{ e.preventDefault(); doRandomize(); }});

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

async function doGenerate() {{
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
        gender: val('gender'),
        style: (document.querySelector("input[name='style']:checked")?.value || ''),
        extra: val('traits'),
      }})
    }});
    if (!resp.ok) {{
      const txt = await resp.text();
      throw new Error(txt);
    }}

    // generation finalized server-side; redirect to Characters
    await resp.blob();
    window.location.href = '/characters?t=' + encodeURIComponent(token);
  }} catch (e) {{
    alert('Error: ' + String(e));
  }} finally {{
    setGenerating(false);
    btn.disabled = false;
  }}
}}

btn.onclick = () => {{ doGenerate(); }};
btn.addEventListener('touchend', (e) => {{ e.preventDefault(); doGenerate(); }});

</script>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n"
