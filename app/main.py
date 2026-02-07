from __future__ import annotations

import os
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="CharGen", version="0.0.1")


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


@app.get("/")
def index():
    return {
        "service": "chargen",
        "status": "ok",
        "next": "Implement UI + generator pipeline",
    }


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n"
