# Wybe Night Shift – Oslo Sprint 2026

AI-agent som styrer en hotellrobot på nattskift. Se [docs/PLAN.md](docs/PLAN.md).

## Kom i gang

```bash
uv venv .venv
uv pip install -p .venv -r requirements.txt
cp .env.example .env   # fyll inn NIGHTSHIFT_KEY
uv run python -m nightshift.explore   # bruker .venv automatisk, ingen aktivering nødvendig
```
