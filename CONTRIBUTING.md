# Contributing

Thanks for helping improve the AI Lead Intelligence Engine!

## Getting started

```bash
git clone https://github.com/izeen-ux/ai-lead-intelligence-engine.git
cd ai-lead-intelligence-engine
uv sync
uv run python scripts/run_dashboard.py --demo --port 8010   # sample data, no keys
```

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app tests scripts
uv run pytest
```

- Keep changes small and focused; one topic per pull request.
- Add or update tests for behaviour you change.
- Never commit secrets: `.env`, `secrets/`, backups and API keys are git-ignored — keep it that way.
- New data sources must work **without** logins, cookies or CAPTCHA bypasses, and read public pages only.
- The hard qualification rules (country, round-size cap, time window, verified identity, verified
  contact before analysis) are deliberate; propose changes in an issue first.

## Reporting bugs

Open an issue with what you ran, what you expected, what happened and the relevant lines of
`docker compose logs app --tail 50` (remove anything private first).
