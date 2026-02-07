#!/usr/bin/env python3
"""CharGen end-to-end smoke test.

Runs against a deployed instance to verify:
- auth works (token or passphrase cookie)
- pages load (characters list + a character detail page)
- JSON endpoints respond (save, quote, optional regenerate)

Usage examples:

  # token mode (phone link token)
  python tools/smoke_test.py --base-url https://char.i0q.com --token "$CHARGEN_TOKEN" --do-quote

  # passphrase mode
  python tools/smoke_test.py --base-url https://char.i0q.com --passphrase "..." --do-quote

Notes:
- --do-regenerate will call the image model (slow/expensive). Off by default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests

UUID_RE = re.compile(r"/c/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


@dataclass
class Cfg:
    base_url: str
    token: Optional[str]
    passphrase: Optional[str]
    timeout: float
    do_quote: bool
    do_regenerate: bool


def die(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(2)


def ok(msg: str) -> None:
    print(f"OK: {msg}")


def normalize_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    return url


def login_with_passphrase(sess: requests.Session, cfg: Cfg) -> None:
    # GET login page
    r = sess.get(cfg.base_url + "/login", timeout=cfg.timeout, allow_redirects=True)
    if r.status_code != 200:
        die(f"GET /login -> {r.status_code}")

    # POST form
    r = sess.post(
        cfg.base_url + "/login",
        data={"passphrase": cfg.passphrase or ""},
        timeout=cfg.timeout,
        allow_redirects=False,
    )
    if r.status_code not in (302, 303):
        die(f"POST /login expected 302/303, got {r.status_code}: {r.text[:200]}")

    loc = r.headers.get("location", "")
    if "/login" in loc and "err=" in loc:
        die("Passphrase rejected (redirected back with err)")

    # follow to /
    if loc:
        r2 = sess.get(cfg.base_url + loc, timeout=cfg.timeout, allow_redirects=True)
        if r2.status_code not in (200, 302):
            die(f"follow login redirect -> {r2.status_code}")

    ok("passphrase login")


def with_token_params(url: str, token: str) -> str:
    sep = "&" if "?" in url else "?"
    return url + f"{sep}t={requests.utils.quote(token)}"


def fetch_characters_page(sess: requests.Session, cfg: Cfg) -> str:
    url = cfg.base_url + "/characters"
    if cfg.token:
        url = with_token_params(url, cfg.token)
    r = sess.get(url, timeout=cfg.timeout)
    if r.status_code != 200:
        die(f"GET /characters -> {r.status_code}: {r.text[:200]}")
    if "Characters" not in r.text:
        die("/characters page missing expected text")
    ok("characters page loads")
    return r.text


def pick_first_character_id(characters_html: str) -> str:
    m = UUID_RE.search(characters_html)
    if not m:
        die("No /c/<uuid> links found on /characters (need at least 1 character)")
    return m.group(1)


def fetch_character_detail(sess: requests.Session, cfg: Cfg, cid: str) -> str:
    url = cfg.base_url + f"/c/{cid}"
    if cfg.token:
        url = with_token_params(url, cfg.token)
    r = sess.get(url, timeout=cfg.timeout)
    if r.status_code != 200:
        die(f"GET /c/{cid} -> {r.status_code}: {r.text[:200]}")
    if "Regenerate image" not in r.text or "Generate Quote" not in r.text:
        die("character detail page missing expected controls")
    ok("character detail page loads")
    return r.text


def post_json(sess: requests.Session, cfg: Cfg, path: str, payload: dict) -> dict:
    url = cfg.base_url + path
    if cfg.token:
        url = with_token_params(url, cfg.token)
    r = sess.post(url, json=payload, timeout=cfg.timeout)
    if r.status_code != 200:
        die(f"POST {path} -> {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        return {"ok": True}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", default=None)
    ap.add_argument("--passphrase", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--do-quote", action="store_true")
    ap.add_argument("--do-regenerate", action="store_true")
    args = ap.parse_args()

    cfg = Cfg(
        base_url=normalize_base(args.base_url),
        token=(args.token or None),
        passphrase=(args.passphrase or None),
        timeout=args.timeout,
        do_quote=bool(args.do_quote),
        do_regenerate=bool(args.do_regenerate),
    )

    if not cfg.token and not cfg.passphrase:
        die("Provide --token or --passphrase")

    sess = requests.Session()
    sess.headers["User-Agent"] = "chargen-smoketest/1.0"

    # Health
    r = sess.get(cfg.base_url + "/ping", timeout=cfg.timeout)
    if r.status_code != 200:
        die(f"GET /ping -> {r.status_code}")
    ok("ping")

    if cfg.passphrase:
        login_with_passphrase(sess, cfg)

    html = fetch_characters_page(sess, cfg)
    cid = pick_first_character_id(html)
    ok(f"found character {cid}")

    fetch_character_detail(sess, cfg, cid)

    # Save (lightweight, should be fast)
    out = post_json(sess, cfg, f"/api/character/{cid}", {"name": "SmokeTest", "extra": "", "traits": ""})
    ok("save endpoint")

    if cfg.do_quote:
        out = post_json(sess, cfg, f"/api/character/{cid}/quote", {})
        if not (out.get("ok") or out.get("quote")):
            die(f"quote endpoint unexpected response: {json.dumps(out)[:200]}")
        ok("quote endpoint")

    if cfg.do_regenerate:
        t0 = time.time()
        out = post_json(
            sess,
            cfg,
            f"/api/character/{cid}/regenerate",
            {"style": "Illustrated fantasy", "extra": "", "traits": "Style: Illustrated fantasy"},
        )
        if not out.get("image_url"):
            die(f"regenerate endpoint missing image_url: {json.dumps(out)[:200]}")
        ok(f"regenerate endpoint ({time.time()-t0:.1f}s)")

    print("\nPASS")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        die(f"network error: {e}")
