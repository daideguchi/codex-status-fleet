# Contributing

Thanks for considering contributing!

## Development setup

- Install Docker Desktop (or Docker Engine on Linux)
- Keep `accounts/` and `accounts.json` local only (they contain auth tokens / account metadata)

Quick start:

```bash
cp accounts.example.json accounts.json
./scripts/up.sh accounts.json
```

## Security & privacy rules (important)

- Do **not** commit `accounts/` (per-account `~/.codex` tokens)
- Do **not** commit `accounts.json` (local config)
- Do **not** commit `data/` (local SQLite)

## Pull requests

- Keep changes focused and small
- Prefer additive changes that keep current Codex-only workflows working
- If you change APIs or DB schema, include a migration story

