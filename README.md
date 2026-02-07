# CharGen

A phone-friendly D&D-style character avatar generator.

## What it does

- **Generate page** (`/`): pick options + free-text details, then generate an avatar (includes style preview placeholders).
- **Characters page** (`/characters`): grid of generated characters.
- **Character details page** (`/c/<uuid>`): edit name/details/traits, regenerate image, delete, generate an in-character quote (appended to details).

## Auth

The app is token-gated.

- Set `CHARGEN_TOKEN`
- Provide it as `?t=...` or `Authorization: Bearer ...`

## Storage

- **Postgres (DO Managed DB):** stores metadata (name/options/details/traits/image_url, timestamps)
- **DigitalOcean Spaces:** stores images under `storyforge-assets/.../chargen/`

## Endpoints

- `GET /ping` health
- `POST /generate` generate + persist (token required)
- `GET /characters` list page (token required)
- `GET /c/<id>` character details page (token required)
- `POST /api/character/<id>` update name/details/traits
- `POST /api/character/<id>/regenerate` regenerate image (uses traits + details)
- `POST /api/character/<id>/delete` delete character + best-effort delete image
- `POST /api/character/<id>/quote` generate short quote (appends to details in UI)

## Runtime env vars (DigitalOcean App Platform)

Required:
- `CHARGEN_TOKEN`
- `GEMINI_API_KEY`
- `DATABASE_URL` (Postgres connection string)
- `SPACES_ACCESS_KEY`
- `SPACES_SECRET_KEY`
- `SPACES_BUCKET` (e.g. `storyforge-assets`)
- `SPACES_REGION` (e.g. `sfo3`)
- `SPACES_ENDPOINT` (e.g. `sfo3.digitaloceanspaces.com`)

Optional:
- `GEMINI_IMAGE_MODEL` (default: `gemini-2.5-flash-image`)
- `GEMINI_TEXT_MODEL` (default: `gemini-2.0-flash`)

## Notes

- Images are currently returned as PNG.
- Generator page redirects to **Characters** after generation completes.
