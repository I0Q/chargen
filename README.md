# CharGen

Minimal v0 web service for character generation.

## Local dev

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Auth

Set `CHARGEN_TOKEN` and pass `?t=...` or `Authorization: Bearer ...`.

## Health

- `GET /ping`
